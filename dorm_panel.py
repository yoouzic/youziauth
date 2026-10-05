"""Independent dormitory UI and worker lifecycle; no network-agent commands."""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import queue
import threading
import time

from dorm_api import SwuApi
from dorm_checkin import CheckinError, Engine, Result, Settings, Store, now, parse_time
from dorm_location import locate
from dorm_login import LOGIN_GATE, login, school_cookies

# 每天最多自动续期登录几次。超过就停手，等人工登录。
#
# 为什么必须有这个上限：自动打卡在时段内每 interval 秒跑一次，一旦会话失效，
# 每次都会走自动续期登录（拉起一个浏览器）。2026-09-24 实测过一次后果：
# 21:02–23:27 连续 2.5 小时、每 5 分钟一次、成功率 0，还伴随大量 Windows 登录失败事件
# 并导致本机账户被锁定。**自动行为必须有硬上限**，不能只靠"应该会成功"。
#
# 这是**每个账号**的上限；多账号还有两层：dorm_login.LoginGate（同时只跑一条登录链路）
# 与 dorm_accounts.MachineLoginBudget（整机每天的自动登录总数）。
MAX_DAILY_LOGIN_RENEWALS = 3


class DormController:
    def __init__(self, store=None, engine=None, idm_store=None, login_gate=None,
                 stagger_seconds=0, login_budget=None):
        self.store = store or Store()
        self.api = SwuApi()
        self.engine = engine or Engine(self.store, self.api, self._locate)
        # 本账号的统一认证凭据存储。多账号下由 DormAccounts 注入「该账号自己的」存储；
        # 没有注入时退回全局默认目录（旧工具、自检与测试用）。
        self.idm_store = idm_store
        # 机器级的登录闸：默认就是进程级单例，所以即使调用方什么都不传，
        # 「同一台电脑同时只跑一条学校登录链路」这条也成立。
        self.login_gate = login_gate if login_gate is not None else LOGIN_GATE
        # 整机每天的自动登录预算（多账号时由 DormAccounts 注入；单账号为 None）。
        self.login_budget = login_budget
        # 错峰：本账号当天第一次自动检查要等窗口开始 + 这个偏移量。
        # 多账号共用一个出口，同一秒一起打学校接口没有任何好处。
        self.stagger_seconds = max(0, int(stagger_seconds or 0))
        self.events = queue.Queue()
        self.busy = False
        self.closed = False
        self._gate = threading.Lock()
        self._poll_at = 0.0
        self._renew_at = 0.0
        self.latest = Result('idle', '尚未查询今日任务')

    def credential_store(self):
        """本账号的统一认证凭据存储。"""
        if self.idm_store is not None:
            return self.idm_store
        from idm_credentials import IdmCredentialStore  # noqa: PLC0415

        return IdmCredentialStore()

    def _locate(self):
        return locate(self.store.settings().location_source, self.store.root / 'location-sample.json')

    def start(self, action):
        if action not in ('query', 'submit', 'login', 'logout', 'automatic'):
            return False
        with self._gate:
            if self.closed or self.busy:
                return False
            self.busy = True
            self.engine.cancel.clear()

        def work():
            try:
                if action == 'login':
                    if not self.login_gate.acquire():
                        # 多账号：别的账号正在跑登录链路。人工操作不排队，直接说清楚。
                        raise CheckinError('busy', '另一个账号正在登录，请等它结束后再试')
                    try:
                        login(self.store, self.api, self.engine.cancel, idm_store=self.idm_store)
                    finally:
                        self.login_gate.release()
                    self._renew_at = 0.0
                    result = Result('logged_in', '登录成功，凭据已在本机加密保存')
                elif action == 'logout':
                    settings = self.store.settings()
                    self.store.save_settings(dataclasses.replace(settings, enabled=False))
                    self.store.clear_token()
                    result = Result('login_required', '登录凭据已清除，自动打卡已关闭')
                else:
                    result = self._run(action)
                if result is not None and result.state not in ('disabled', 'waiting'):
                    self.events.put(result)
                elif result is not None and action != 'automatic':
                    self.events.put(result)
            except CheckinError as exc:
                self.events.put(Result(exc.state, str(exc)))
            except Exception:
                self.events.put(Result('error', '打卡操作未完成，请检查配置和网络后重试'))
            finally:
                with self._gate:
                    self.busy = False

        threading.Thread(target=work, name='youziauth-dorm-' + action, daemon=True).start()
        return True

    def _check_window(self):
        """返回 (start, end) 文本；读不到设置就返回 None。"""
        try:
            settings = self.store.settings()
            return settings.start, settings.end
        except Exception:  # noqa: BLE001
            return None

    def _inside_check_window(self):
        """当前是否在自动检查时段内。

        读不到设置时返回 False —— 宁可不开浏览器，也不要在时段外自作主张。
        """
        window = self._check_window()
        if window is None:
            return False
        start, end = window
        try:
            return parse_time(start) <= self.engine.clock().time() < parse_time(end)
        except ValueError:
            return False

    def _outside_window_message(self):
        window = self._check_window()
        span = f'{window[0]}–{window[1]}' if window else '设定时段'
        return (f'当前不在自动打卡时段（{span}），已停止自动登录；'
                '请点「学校登录 / 重新登录」手动完成')

    def _run(self, action):
        automatic = action == 'automatic'
        result = self.engine.tick() if automatic else self.engine.run(submit=action == 'submit')
        if result is None or result.state != 'login_required' or time.monotonic() < self._renew_at:
            return result
        self._renew_at = time.monotonic() + 300
        # 只在检查时段内自动登录：用户要求「每天晚上 21 点才开始尝试自动登录」。
        #
        # 为什么需要这道闸：自动打卡在时段外会被 Engine 直接判为 waiting（不碰网络），
        # 但**人工**在白天点一次「查询今日任务」也会得到 login_required，
        # 而那同样会走到下面这段续期登录、在无人值守的情况下拉起一个浏览器。
        # 时段外一律不偷偷开浏览器，直接告诉人手动登录。
        if not self._inside_check_window():
            return Result('login_required', self._outside_window_message())
        # ★ 无人值守这条路走不通时（既没会话又没凭据）先说清楚：那一趟注定失败，
        #   却照样会记账、花整机预算。2026-10-05 实测：一个从没登录过的账号 5 分钟内
        #   空转两次，吃掉自己 3 次额度里的 2 次和整机 6 次里的 2 次，界面还只给一句
        #   含糊的「没有可恢复的学校会话」。
        blocker = self._unattended_login_blocker()
        if blocker:
            return self._skip_login(blocker, result)
        # 熔断：每天自动续期登录有硬上限。到顶就不再拉起浏览器，直接告诉人去手动登录。
        # 只对自动打卡生效 —— 人工点「登录 / 重新登录」永远不受限制。
        today = self.engine.clock().date().isoformat()
        try:
            used = self.store.daily_attempts(today, 'login_renewal')
        except Exception:  # noqa: BLE001
            used = 0
        if automatic and used >= MAX_DAILY_LOGIN_RENEWALS:
            return self._skip_login(
                f'今天已自动尝试登录 {used} 次仍未成功，已停止自动重试；'
                '请点「学校登录 / 重新登录」手动完成，或检查已保存的凭据', result)
        # 整机预算：多账号共用一个出口，一台电脑每天自动拉起的浏览器总数必须有上限。
        # 先查预算再记账，最后才过登录闸 —— 顺序很关键：**被挡下的那一拍不算一次尝试**，
        # 否则额度会被排队白白烧光，真该重试的时候反而没机会了。
        if automatic and self.login_budget is not None:
            reason = self.login_budget.check(today)
            if reason:
                return self._skip_login(reason, result)
        if not self.login_gate.acquire():
            # 别的账号正在登录：不排队（一次登录最长 5 分钟，等在闸后既不会更快，
            # 还会把当天的额度耗在等待上），这一拍直接跳过，_renew_at 到点自然会再试。
            return Result('waiting', '等待其他账号完成登录')
        try:
            if automatic and self.login_budget is not None:
                self.login_budget.spend(today)
            try:
                self.store.bump_daily_attempts(today, 'login_renewal')
            except Exception:  # noqa: BLE001
                pass                  # 记不上也照常尝试，只是上限保护会失效一次
            try:
                login(self.store, self.api, self.engine.cancel, interactive=False,
                      idm_store=self.idm_store)
            except CheckinError as exc:
                result = Result(exc.state, str(exc), result.task, now().isoformat(timespec='seconds'))
                self.store.record(result)
                return result
        finally:
            self.login_gate.release()
        # A known task may already have been submitted; renewal must never replay that write.
        result = self.engine.run(automatic=automatic, submit=(automatic or action == 'submit') and result.task is None)
        if result.state == 'login_required':
            self.store.clear_browser_session()
        return result

    def _skip_login(self, message, result):
        """这一拍不动登录：把原因也写进状态与历史，再报给上层。

        为什么不只是 return：history.log / status.json 是事后唯一的现场记录
        （2026-10-05 实测：界面说「已停止自动重试」，磁盘上却只有引擎那句
        「请先登录统一身份认证」，看不出到底为什么停）。连续同一条会被 history 折叠，
        所以每 5 分钟重复一次也不会把记录刷满。
        """
        skipped = Result('login_required', message, result.task, now().isoformat(timespec='seconds'))
        try:
            self.store.record(skipped)
        except Exception:  # noqa: BLE001 - 写不进记录也不能影响这一拍的结论
            pass
        return skipped

    def _unattended_login_blocker(self):
        """无人值守登录这条路走不走得通；走不通就返回一句「该让人做什么」的原因。

        走得通的有两种（用户 2026-10-05 明确选择允许第二种）：
          * 手上还有没过期的学校 cookie —— 续期，最省事也最稳；
          * 没有会话但保存了统一认证凭据 —— headless 走完整登录流程
            （自动填学号密码 + 本地模型识别验证码），仍然只在时段内、受同一套
            登录闸与每天上限约束。
        两种都不成立时必须先拦下来：那一趟注定失败，却照样会记账、花整机预算
        （2026-10-05 实测：一个从没登录过的账号空转两次，吃掉自己 3 次额度里的 2 次
        和整机 6 次里的 2 次，界面还只给一句含糊的「没有可恢复的学校会话」）。
        这里只读本机状态，不碰网络、不开浏览器。
        """
        try:
            token = self.store.token()
            session = self.store.browser_session(token) if token else None
        except CheckinError:
            return '登录凭据无法解密：请点「学校登录 / 重新登录」重新登录一次'
        if session and school_cookies(session.get('cookies') or []):
            return ''
        if self._has_idm_credentials():
            return ''
        if token:
            return ('学校会话已过期，也没有可用的统一认证凭据：请点「学校登录 / 重新登录」重新登录一次，'
                    '或先在面板里保存该账号的统一认证凭据')
        return ('这个账号还没有登录过，也没有保存统一认证凭据：请点「学校登录 / 重新登录」完成一次登录，'
                '或先在面板里保存该账号的统一认证凭据')

    def _has_idm_credentials(self):
        """本账号有没有可用的统一认证凭据。读不出来就当没有（fail-closed）。"""
        try:
            store = self.credential_store()
            return bool(store.exists() and store.load())
        except Exception:  # noqa: BLE001 - 凭据读不出来只影响「能不能无人值守」，不能影响打卡本身
            return False

    def _stagger_ready(self, settings):
        """错峰：本账号当天第一次自动检查要等到「窗口开始 + 自己的偏移量」。

        偏移量由 DormAccounts 按账号在清单里的顺序分配（0、90、180… 秒）。此时段本身
        仍由 Engine 判定，这里只是把多账号的**第一拍**错开，避免同一秒一起打学校接口。
        时段读不动时返回 True：那是 Engine 该 fail-closed 的事，不在这里替它做决定。
        """
        if not self.stagger_seconds:
            return True
        try:
            start = parse_time(settings.start)
        except ValueError:
            return True
        current = self.engine.clock()
        due = dt.datetime.combine(current.date(), start) + dt.timedelta(seconds=self.stagger_seconds)
        return current.time() >= due.time()      # datetime.time() 不带 tzinfo，两边同为本地时刻

    def poll(self):
        if self.closed or time.monotonic() < self._poll_at:
            return
        self._poll_at = time.monotonic() + 5
        try:
            settings = self.store.settings()
            if settings.enabled and self._stagger_ready(settings):
                self.start('automatic')
        except Exception:
            if self.latest.state != 'error':
                self.events.put(Result('error', '打卡设置无法读取，请打开寝室打卡设置重新保存'))

    def drain(self):
        values = []
        while True:
            try:
                self.latest = self.events.get_nowait()
                values.append(self.latest)
            except queue.Empty:
                return values

    def save(self, settings):
        with self._gate:
            if self.closed or self.busy:
                raise RuntimeError('请等待当前打卡操作完成后再保存设置。')
            self.store.save_settings(settings)
            self.engine.next_tick = 0
            self._poll_at = 0

    def cancel(self):
        self.engine.cancel.set()

    def close(self):
        with self._gate:
            self.closed = True
            self.cancel()

    def schedule_text(self):
        try:
            s = self.store.settings()
            if not s.enabled:
                return '自动打卡：关闭'
            current = now()
            start, end = parse_time(s.start), parse_time(s.end)
            if current.time() < start:
                due = current.replace(hour=start.hour, minute=start.minute, second=0)
            elif current.time() >= end:
                due = (current + dt.timedelta(days=1)).replace(hour=start.hour, minute=start.minute, second=0)
            else:
                due = current + dt.timedelta(seconds=max(0, self.engine.next_tick-time.monotonic()))
                if due.time() >= end:
                    due = (current + dt.timedelta(days=1)).replace(hour=start.hour, minute=start.minute, second=0)
            return f'自动打卡：开启 · 下次检查 {due:%m-%d %H:%M}（北京时间）'
        except Exception:
            return '自动打卡：配置无效'


class DormPanel:
    def __init__(self, parent, controller):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.controller = controller
        self.window = tk.Toplevel(parent)
        self.window.title('youziauth · 寝室打卡')
        self.window.geometry('700x650')
        self.window.minsize(660, 630)
        self.window.protocol('WM_DELETE_WINDOW', self.window.withdraw)
        frame = ttk.Frame(self.window, padding=20)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text='寝室打卡', font=('Microsoft YaHei UI', 16, 'bold')).grid(row=0, sticky='w')
        ttk.Label(frame, text='使用学校统一身份认证登录，与校园网账号设置分开。', wraplength=620).grid(row=1, sticky='w', pady=(6, 12))
        self.state = tk.StringVar(value=controller.latest.message)
        self.task_text = tk.StringVar(value='今日任务：尚未查询')
        self.schedule = tk.StringVar()
        ttk.Label(frame, textvariable=self.state, wraplength=620).grid(row=2, sticky='w', pady=4)
        ttk.Label(frame, textvariable=self.task_text, wraplength=620, justify='left').grid(row=3, sticky='w', pady=4)
        ttk.Label(frame, textvariable=self.schedule, wraplength=620).grid(row=4, sticky='w', pady=(4, 12))

        actions = ttk.Frame(frame)
        actions.grid(row=5, sticky='ew')
        self.buttons = []
        for column, (label, action) in enumerate((('登录 / 重新登录', 'login'), ('查询今日任务', 'query'), ('提交今日打卡', 'submit'))):
            button = ttk.Button(actions, text=label, command=lambda a=action: self.act(a))
            button.grid(row=0, column=column, padx=(0, 8), pady=4)
            self.buttons.append(button)
        ttk.Button(actions, text='取消当前操作', command=controller.cancel).grid(row=1, column=0, sticky='w', pady=4)
        logout = ttk.Button(actions, text='清除登录凭据', command=lambda: self.act('logout'))
        logout.grid(row=1, column=1, sticky='w', pady=4)
        self.buttons.append(logout)
        ttk.Button(actions, text='Windows 定位设置', command=self.open_location_settings).grid(row=1, column=2, pady=4)

        # 统一认证（IDM）凭据：用于自动填写账号密码 + 自动识别验证码。
        # 不填也能用，只是每次登录都需要人工输入。
        self.idm_box = ttk.LabelFrame(frame, text='统一认证静默登录（可选）', padding=12)
        self.idm_box.grid(row=6, sticky='ew', pady=(12, 0))
        self.idm_status = tk.StringVar()
        ttk.Label(self.idm_box, textvariable=self.idm_status, wraplength=620,
                  justify='left').grid(row=0, column=0, columnspan=4, sticky='w')
        ttk.Label(self.idm_box, text='说明：凭据用 Windows DPAPI 加密后仅存本机，'
                                     '仅供自动登录使用；可在本机被完全控制时被解密。',
                  wraplength=620, justify='left').grid(row=1, column=0, columnspan=4, sticky='w', pady=(4, 8))
        ttk.Label(self.idm_box, text='学号').grid(row=2, column=0, sticky='w')
        self.idm_user = tk.StringVar()
        ttk.Entry(self.idm_box, width=22, textvariable=self.idm_user).grid(row=2, column=1, padx=5, sticky='w')
        ttk.Label(self.idm_box, text='密码').grid(row=2, column=2, sticky='w')
        self.idm_pass = tk.StringVar()
        ttk.Entry(self.idm_box, width=22, textvariable=self.idm_pass, show='•').grid(row=2, column=3, padx=5, sticky='w')
        ttk.Button(self.idm_box, text='保存凭据', command=self.save_idm_credentials).grid(row=3, column=1, pady=6, sticky='w')
        self.idm_clear = ttk.Button(self.idm_box, text='清除凭据', command=self.clear_idm_credentials)
        self.idm_clear.grid(row=3, column=3, pady=6, sticky='w')
        self.buttons.append(self.idm_clear)

        options = ttk.LabelFrame(frame, text='自动打卡', padding=12)
        options.grid(row=7, sticky='ew', pady=12)
        self.enabled = tk.BooleanVar(value=False)
        self.start = tk.StringVar(value='21:00')
        self.end = tk.StringVar(value='23:15')
        self.interval = tk.StringVar(value='300')
        ttk.Checkbutton(options, text='启用自动打卡（保存后生效）', variable=self.enabled).grid(row=0, column=0, columnspan=4, sticky='w')
        ttk.Label(options, text='检查时段').grid(row=1, column=0, sticky='w', pady=8)
        ttk.Entry(options, width=8, textvariable=self.start).grid(row=1, column=1, padx=5)
        ttk.Label(options, text='至').grid(row=1, column=2)
        ttk.Entry(options, width=8, textvariable=self.end).grid(row=1, column=3, padx=5)
        ttk.Label(options, text='间隔（秒）').grid(row=2, column=0, sticky='w')
        ttk.Entry(options, width=8, textvariable=self.interval).grid(row=2, column=1, padx=5)
        ttk.Button(options, text='保存打卡设置', command=self.save).grid(row=2, column=3, padx=5)
        self.location_text = tk.StringVar()
        ttk.Label(frame, textvariable=self.location_text,
                  wraplength=620, justify='left').grid(row=8, sticky='w', pady=(0, 10))
        self.history = tk.Text(frame, height=7, wrap='word', state='disabled', font=('Microsoft YaHei UI', 9))
        self.history.grid(row=9, sticky='nsew')
        frame.rowconfigure(9, weight=1)
        self._history_text = None
        self.load_settings()
        self.refresh_idm_status()
        self.refresh()

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def load_settings(self):
        try:
            settings = self.controller.store.settings()
            self.enabled.set(settings.enabled)
            self.start.set(settings.start)
            self.end.set(settings.end)
            self.interval.set(str(settings.interval))
        except Exception:
            self.state.set('配置无法读取，请重新保存打卡设置')

    def save(self):
        from tkinter import messagebox
        try:
            try:
                previous = self.controller.store.settings()
            except (ValueError, TypeError, OSError):
                previous = Settings()
            settings = dataclasses.replace(previous, enabled=self.enabled.get(),
                                           start=self.start.get().strip(), end=self.end.get().strip(),
                                           interval=int(self.interval.get()))
            self.controller.save(settings)
            self.state.set('打卡设置已保存')
        except (ValueError, OSError):
            messagebox.showerror('无法保存', '请填写正确时段和 60–3600 秒间隔，并确认目录可写。', parent=self.window)
        except RuntimeError as exc:
            messagebox.showerror('无法保存', str(exc), parent=self.window)
        self.refresh()

    def refresh_idm_status(self):
        """显示统一认证凭据的存储状态（不显示密码本身）。"""
        try:
            store = self.controller.credential_store()
            if not store.exists():
                self.idm_status.set('未保存统一认证凭据：每次登录需人工输入账号密码和验证码。')
                self.idm_clear.state(['disabled'])
                return
            creds = store.load()
            name = creds.username if creds else ''
            self.idm_status.set(f'已保存统一认证凭据（学号 {name}）：登录时将自动填写并识别验证码。')
            if creds:
                self.idm_user.set(creds.username)
            self.idm_clear.state(['!disabled'])
        except Exception as exc:  # noqa: BLE001
            self.idm_status.set(f'统一认证凭据状态未知：{exc}')
            self.idm_clear.state(['disabled'])

    def save_idm_credentials(self):
        from tkinter import messagebox
        try:
            from idm_credentials import IdmCredentials
            creds = IdmCredentials(username=self.idm_user.get().strip(),
                                   password=self.idm_pass.get()).validate()
            self.controller.credential_store().save(creds)
        except ValueError as exc:
            messagebox.showerror('无法保存', str(exc), parent=self.window)
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror('无法保存', f'凭据加密保存失败：{exc}', parent=self.window)
            return
        # 保存成功后立刻清空密码输入框，避免明文长期停留在界面变量中
        self.idm_pass.set('')
        self.refresh_idm_status()
        self.state.set('统一认证凭据已加密保存，可用于自动登录')

    def clear_idm_credentials(self):
        from tkinter import messagebox
        if not messagebox.askyesno('清除凭据',
                                   '确定清除已保存的统一认证凭据？\n清除后登录需要人工输入账号密码和验证码。',
                                   parent=self.window):
            return
        try:
            self.controller.credential_store().clear()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror('无法清除', str(exc), parent=self.window)
            return
        self.idm_user.set('')
        self.idm_pass.set('')
        self.refresh_idm_status()
        self.state.set('统一认证凭据已清除')

    def act(self, action):
        if self.controller.start(action):
            self.state.set('请在新打开的浏览器中完成学校登录（最多等待 5 分钟）' if action == 'login' else '正在处理，请稍候…')
            self.refresh()

    def open_location_settings(self):
        try:
            os.startfile('ms-settings:privacy-location')
        except OSError:
            self.state.set('请手动打开 Windows 设置 → 隐私和安全性 → 位置')

    def update(self, result):
        self.state.set(result.message)
        if result.task:
            t = result.task
            self.task_text.set(f'今日任务：{t.title}\n日期：{t.date} · 学校时段：{t.start}–{t.end}\n地点：{t.address or "学校未提供地址"}')
        elif result.state in ('logged_in', 'login_required', 'no_task'):
            self.task_text.set('今日任务：请查询' if result.state != 'no_task' else '今日任务：暂无')
        if result.state == 'login_required':
            self.load_settings()
        self.refresh()

    def refresh(self):
        self.schedule.set(self.controller.schedule_text())
        try:
            source = self.controller.store.settings().location_source
            description = ('模拟定位（本机样本随机偏移，非实时，并非当前位置）' if source == 'simulation' else
                           'Windows 实时定位（由系统选择来源）')
            self.location_text.set(f'全局定位来源：{description}。检测和提交均沿用此选择，仍须通过学校范围校验。\n'
                                   '自动执行需电脑开机、登录 Windows，并保持软件运行；位置不可用或精度不足时停止。')
        except (ValueError, TypeError, OSError):
            self.location_text.set('定位来源配置不可用，请在主界面重新保存设置。')
        for button in self.buttons:
            button.configure(state='disabled' if self.controller.busy else 'normal')
        try:
            text = self.controller.store.history() or '暂无打卡记录'
        except OSError:
            text = '无法读取打卡记录'
        if text != self._history_text:
            self._history_text = text
            self.history.configure(state='normal')
            self.history.delete('1.0', 'end')
            self.history.insert('1.0', text)
            self.history.configure(state='disabled')
            self.history.see('end')
