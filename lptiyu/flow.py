"""每日流水线(dry-run 默认; 提交需显式授权)。

时序对齐真机:
  0 真门 getTermList(假阳性源 getSchoolInfo 弃用为门) + 学校信息
  → 1 beforeRunV260(跑区/打卡点/时间规则) [可选 setRunZone]
  → 2 当日额度(term_records 两页)
  → 3 服务器时间 → 4 轨迹生成(重试保证覆盖+窗口) → 5 时间轴/计步
  → 6 record_file 命名 → [dry-run 到此为止, 零写入]
  → 7 getOssSts + OSS 上传 → 8 stopRunV278(flag) → 9 recordDetailV270 复核

与 lptiyu-lab 原型的差异(有意为之):
  * 提交成功后不再把 token 记为「已消耗」: 2026-11-11 实测 token 的死亡
    只由下一次 loginByCode 轮换触发, 与是否提交无关。改为 after_submission()
    由槽位自行探测 —— 活着就保留给下一次, 101 才判死。这是 token 持久化
    的核心: 凭证不再是一次性消耗品。
  * 模板缺失自动落到 ring 引擎, 不再是致命错误。
"""
import json
import random
import threading
import time

from . import spec
from . import tracks as TRK
from . import upfile as UP
from .cipher import seal
from .client import ApiError
from .spec import R_AUTOCORRECT, R_NEGATIVE_HINTS


def _positive_reason(reason: str) -> bool:
    """record_failed_reason 是坑字段: 服务端也用它装肯定判定文本
    ("自动确认有效"=过审, 与真人记录一致; 否定例: "打卡点异常")。"""
    r = (reason or "").strip()
    return (not r) or ("有效" in r and not any(h in r for h in R_NEGATIVE_HINTS)
                       ) or (R_AUTOCORRECT in r)


def pace_text(seconds_per_km):
    """秒/公里 → 4'05" 形态; 输入不合法返回空串。"""
    try:
        s = int(round(float(seconds_per_km)))
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    return f"{s // 60}'{s % 60:02d}\""


class DailyFlow:
    def __init__(self, slot, policy=None, verbose=True, stop_event=None,
                 log_fn=None, template_path="", zone_profile=None, zone_tuning=None,
                 zone_sink=None):
        self.slot = slot
        # zone_tuning: 该跑区的人工特调(方向/贴点半径/跑道偏移); 每次读到真机
        # 点位后由 zones.profile() 折成 zone_profile。zone_sink 让调用方留档。
        self.zone_profile = dict(zone_profile or {})
        self.zone_tuning = dict(zone_tuning) if zone_tuning is not None else None
        self.zone_sink = zone_sink
        # pace_lo/pace_hi = 用户指定的配速区间(秒/公里, 0/0=交给模板);
        # pace_min/pace_max = 引擎自己的人工配速合理区间, 只用来兜住「不像人跑」的结果。
        self.policy = dict({"max_per_day": 1, "style": "template",
                            "pace_lo": 0, "pace_hi": 0,
                            "pace_min": 185, "pace_max": 560,
                            "dist_lo": 2.03, "dist_hi": 2.35}, **(policy or {}))
        self.v = verbose
        self.log_fn = log_fn or (lambda msg: None)   # UI/无控制台环境必须可注入
        self.template_path = template_path or ""
        self.rnd = random.Random()
        self.stop_event = stop_event or threading.Event()
        self.lines = []                              # 本次运行的过程日志(留档)

    def _pace_window(self):
        """用户配速区间 (lo, hi) 秒/公里; (0, 0) = 不指定, 交给模板/默认。

        pace_target 是 1.9.0 的单点写法, 外部调用方可能还在用: 当成上下限相同。
        """
        try:
            lo = int(self.policy.get("pace_lo") or 0)
            hi = int(self.policy.get("pace_hi") or 0)
        except (TypeError, ValueError):
            return (0, 0)
        if lo and hi and lo <= hi:
            return (lo, hi)
        try:
            legacy = int(self.policy.get("pace_target") or 0)
        except (TypeError, ValueError):
            legacy = 0
        return (legacy, legacy) if legacy and not (lo or hi) else (0, 0)

    # ------------------------------------------------------------ 基础
    def log(self, msg):
        self.lines.append(msg)
        if self.v:
            try:
                self.log_fn(f"[{self.slot.label}] {msg}")
            except Exception:          # pragma: no cover
                pass

    def _sleep(self, sec):
        if sec <= 0:
            return
        t0 = time.time()
        while time.time() - t0 < sec:
            if self.stop_event.is_set():
                raise ApiError(9, "interrupted (user stop)")
            time.sleep(0.2)

    # ------------------------------------------------------------ 步骤
    def _gate(self, client):
        """[0] 真门 + 学校信息。"""
        client.term_list()              # 101 已由 ApiClient 统一抛出
        info = client.school_info()
        if not isinstance(info, dict) or "school_name" not in info:
            raise ApiError(4, f"学校信息异常: {str(info)[:200]}")
        self.log(f"[0] token 有效(真门) | {info.get('school_name')}")
        self._sleep(self.rnd.uniform(0.6, 1.5))

    def _zone_rule(self, client):
        cfg = client.before_run()
        if not isinstance(cfg, dict) or not cfg.get("run_line_info"):
            raise ApiError(4, f"beforeRun 异常: {str(cfg)[:200]}")
        pts = []
        for p in cfg["run_line_info"].get("point_list") or []:
            try:
                lat, lng = TRK.parse_jingwei(str(p.get("jingwei") or "").strip())
                pts.append((lat, lng, str(p.get("id"))))
            except Exception:          # noqa: BLE001
                continue
        if len(pts) < 2:
            raise ApiError(4, "跑区打卡点缺失")
        rule0 = (cfg.get("time_rule_arr") or [{}])[0]
        if self.zone_tuning is not None:
            # 用这一次真机返回的点位算场地几何: 每块场地的朝向/宽窄都不同,
            # 贴点半径、跑道与起步方位必须按场地算, 不能吃全局默认。
            from . import zones as zone_geom
            self.zone_profile = zone_geom.profile(
                [(p[0], p[1]) for p in pts], self.zone_tuning,
                zone_id=cfg.get("run_zone_id"))
            self.log(f"[1a] 场地 长轴={self.zone_profile.get('long_axis_deg')}° "
                     f"长宽={self.zone_profile.get('length_m')}x"
                     f"{self.zone_profile.get('width_m')}m "
                     f"方向={self.zone_profile.get('direction')} "
                     f"跑道={self.zone_profile.get('lane_m')}m "
                     f"贴点={self.zone_profile.get('anchor_m')}m")
        rule = {
            "zone": str(cfg.get("run_zone_id") or ""),
            "zone_name": str(cfg.get("run_zone_name") or ""),
            "term_id": str(cfg.get("term_id") or "0"),
            "pts": pts,
            "min_d": float(rule0.get("min_distance") or 2.0),
            "max_d": float(rule0.get("max_distance") or 10.0),
        }
        if self.zone_sink:      # 跑区点位与清单: 界面据此为每块场地做选点特调
            try:
                self.zone_sink(rule["zone"], rule["zone_name"],
                               [{"id": p[2], "lat": p[0], "lng": p[1]} for p in pts],
                               cfg.get("run_zone_list") or [])
            except Exception:   # noqa: BLE001 —— 留档失败不该拦住这一次跑步
                pass
        self.log(f"[1] 跑区={rule['zone']}({rule['zone_name']}) "
                 f"打卡点x{len(pts)} 学期={rule['term_id']} "
                 f"距离窗={rule['min_d']}~{rule['max_d']}km")
        return rule

    def _quota(self, client, term_id, submit):
        """[2] 当日额度(两页)。失效记录(dist=0)不占位, 由服务端裁定。"""
        self._sleep(self.rnd.uniform(0.5, 1.3))
        import datetime
        day0 = int(time.mktime(datetime.date.today().timetuple()))
        done = 0
        for page in (1, 2):
            r = client.term_records(term_id, page)
            if not isinstance(r, dict):
                break
            for rec in r.get("list", []):
                try:
                    st = int(rec.get("start_time") or 0)
                    dist = float(rec.get("distance") or 0)
                except (TypeError, ValueError):
                    continue
                if st >= day0 and dist > 0:
                    done += 1
            if not r.get("list"):
                break
            self._sleep(self.rnd.uniform(0.6, 1.4))
        self.log(f"[2] 今日已完成 {done} 次 (上限 {self.policy['max_per_day']})")
        if done >= self.policy["max_per_day"] and submit:
            raise ApiError(3, "当日额度已满, 不提交")
        return done

    def _gen_track(self, rule, style):
        """返回 (track, kind, used_s, dist_km, meta)。重试保证覆盖+窗口。

        配速区间(pace_lo~pace_hi 秒/公里)对两个引擎都生效:
          * template: 直接按区间采样每公里用时, 反推全程 used(模板点只提供形状);
          * ring: 引擎有自己的速度曲线, 生成后把时间轴整体缩放, 让实配速落进区间。
        模板不可用时自动落到 ring 引擎: ring 不依赖个人数据, 永远可用。
        """
        lo = max(rule["min_d"] + 0.02, self.policy["dist_lo"])
        hi = min(self.policy["dist_hi"], rule["max_d"] - 0.02)
        pace_lo, pace_hi = self._pace_window()
        # 跑区特调优先于全局采点精度: 窄场地必须贴得更紧
        anchor_max = float(self.zone_profile.get("anchor_m")
                           or self.policy.get("anchor_max_m",
                                              spec.CHECKIN_RADIUS_M))
        if style == "template":
            try:
                for attempt in range(10):
                    target = self.rnd.uniform(lo, hi)
                    anchor_dicts = [{"point_id": p[2], "latitude": p[0],
                                     "longtitude": p[1]} for p in rule["pts"]]
                    gen = TRK.TemplateShift(anchor_dicts, distance_km=target,
                                            seed=self.rnd.randint(0, 10 ** 9),
                                            template_path=self.template_path,
                                            profile=self.zone_profile)
                    track, meta = gen.generate(return_meta=True)
                    dist_km = meta["path_m"] / 1000.0
                    covered = all(c <= anchor_max for c in meta["anchor_cover"])
                    self.log(f"[4.{attempt}] tpl 点={len(track)} 距离={dist_km:.2f}km "
                             f"锚点近距={meta['anchor_cover']}")
                    if covered and rule["min_d"] <= dist_km <= rule["max_d"]:
                        if pace_lo:
                            pace = self.rnd.uniform(pace_lo, pace_hi)
                            used = max(60, int(round(dist_km * pace)))
                        else:
                            used = TRK.used_seconds(track)
                            if not (self.policy["pace_min"] <= used / dist_km
                                    <= self.policy["pace_max"]):
                                used = int(dist_km * self.rnd.uniform(230, 480))
                        self.log(f"[4] tpl 用时={used}s 实配速={used / dist_km:.1f}s/km "
                                 f"(区间={f'{pace_lo}-{pace_hi}' if pace_lo else 'auto'})")
                        return track, "template", used, dist_km, meta
            except FileNotFoundError:
                self.log("[4] 模板缺失 → 自动改用 ring 引擎")
            style = "ring"
        for attempt in range(12):
            target_m = self.rnd.uniform(lo, hi) * 1000
            track, crossings, used, dist_km = TRK.ring_track(
                rule["pts"], target_m, self.rnd, profile=self.zone_profile)
            pace = used / dist_km if dist_km > 0 else 9e9
            covered = all(p[2] in crossings for p in rule["pts"])
            self.log(f"[4.{attempt}] ring 点={len(track)} 距离={dist_km}km "
                     f"用时={used}s 配速={pace:.0f}s/km 覆盖={covered}")
            if covered and rule["min_d"] <= dist_km <= rule["max_d"] \
                    and self.policy["pace_min"] <= pace <= self.policy["pace_max"]:
                scale = 1.0
                if pace_lo:
                    # ring 的速度曲线是引擎自己的; 把时间轴整体缩放, 让实配速落进区间。
                    want = max(60, int(round(dist_km * self.rnd.uniform(pace_lo, pace_hi))))
                    scale = want / max(used, 1)
                    used = want
                    self.log(f"[4] ring 时间轴缩放 x{scale:.3f} → "
                             f"实配速={used / dist_km:.1f}s/km "
                             f"(区间={pace_lo}-{pace_hi})")
                return track, "ring", used, dist_km, {"crossings": crossings,
                                                      "time_scale": scale}
        raise ApiError(4, f"轨迹生成失败 style={style} (重试耗尽)")

    # ------------------------------------------------------------ 主入口
    def run(self, submit=False, zone_override=None):  # noqa: C901 —— 线性流水线
        """返回 (summary|None, exit_code)。退出码: 0 成功/受理 | 2 token失效
        | 3 额度满 | 4 其他失败 | 5 受理但判无效 | 9 中断。"""
        client = self.slot.make_client(pace=0.8)
        try:
            self._gate(client)
            rule = self._zone_rule(client)
            if zone_override and zone_override != rule["zone"]:
                client.set_zone(zone_override)
                self.log(f"[1b] setRunZone → {zone_override}")
                rule = self._zone_rule(client)
                if rule["zone"] != zone_override:
                    # 切换被拒时 beforeRun 仍是旧跑区: 拿旧点位冒充新跑区提交必被判无效
                    raise ApiError(4, f"跑区切换未生效: 请求 {zone_override}, "
                                      f"服务器仍在 {rule['zone']}({rule['zone_name']})")
            self._quota(client, rule["term_id"], submit)
            self._sleep(self.rnd.uniform(0.6, 1.4))
            srv_now = client.server_time()
            self.log(f"[3] 服务器时间={srv_now}")
            # 课外跑有效时段守卫(校方规则 06:00~23:00): 时段外提交=白判无效
            t_lo = str(self.policy.get("time_lo", "06:00"))
            t_hi = str(self.policy.get("time_hi", "23:00"))
            hhmm = time.strftime("%H:%M", time.localtime(srv_now))
            if not (t_lo <= hhmm <= t_hi):
                if submit:
                    raise ApiError(4, f"课外跑有效时段 {t_lo}~{t_hi} 之外"
                                      f"(服务器时间 {hhmm}) → 不提交")
                self.log(f"[WARN] 当前 {hhmm} 在有效时段 {t_lo}~{t_hi} 之外"
                         f"(dry-run 继续; 真提交会被拒)")

            style = self.policy.get("style", "template")
            track, kind, used, dist_km, meta = self._gen_track(rule, style)
            end_time = srv_now - self.rnd.randint(15, 75)
            start_time = end_time - used
            if kind == "template":
                anchor_dicts = [{"point_id": p[2], "latitude": p[0],
                                 "longtitude": p[1]} for p in rule["pts"]]
                log_rows = TRK.log_data_template(track, anchor_dicts,
                                                 start_time, end_time)
            else:
                log_rows = TRK.log_data_ring(rule["pts"], track,
                                             meta["crossings"], start_time,
                                             scale=meta.get("time_scale", 1.0))
            steps = TRK.step_info(used)
            step_num = int(sum(steps["list"]))

            track_text = seal(json.dumps(track, ensure_ascii=False,
                                         separators=(",", ":")))
            now_ms, record_file, _fname = UP.make_record_name(self.rnd)
            biz = {
                "term_id": 1,
                "game_id": rule["zone"],
                "start_time": int(start_time),
                "end_time": int(end_time),
                "distance": float(dist_km),
                "record_img": "",
                "log_data": json.dumps(log_rows, ensure_ascii=False,
                                       separators=(",", ":")),
                "file_img": "",
                "is_running_area_valid": 1,
                "mobileDeviceId": 1,
                "mobileModel": 1,
                "mobileOsVersion": 1,
                "step_info": json.dumps(steps, separators=(",", ":")),
                "step_num": step_num,
                "used_time": int(used),
                "record_file": record_file,
            }
            self.log(f"[6] record_file={record_file} txt={len(track_text)}B "
                     f"点数={len(track)}")

            if not submit:
                out = {"stopRun_biz": biz, "track_points": len(track),
                       "track": track,
                       "track_head": track[:3], "track_tail": track[-2:],
                       "log_data": log_rows, "oss_record_file": record_file,
                       "meta": {k: v for k, v in meta.items()
                                if k != "crossings"}}
                self.log("[dry-run] 全参数已生成 (零写入: 未取STS/未上传/未提交)")
                return out, 0

            self._sleep(self.rnd.uniform(0.4, 1.2))
            sts = client.oss_sts()
            if not isinstance(sts, dict) or "AccessKeySecret" not in sts:
                raise ApiError(4, f"getOssSts 异常: {str(sts)[:200]}")
            status, rbody = UP.upload_to_oss(track_text, sts, now_ms,
                                             record_file, self.rnd)
            self.log(f"[6b] OSS 上传 status={status}")
            if status not in (200, 203, 204):
                raise ApiError(4, f"OSS 上传失败: {rbody[:600]}")

            self._sleep(self.rnd.uniform(0.8, 2.4))
            self.log(f"[7] stopRun 距离={dist_km}km 用时={used}s "
                     f"步数={step_num} 配速={int(used / dist_km)}s/km")
            resp = client.stop_run(biz)
            self.log("[7] 响应: " + json.dumps(resp, ensure_ascii=False)[:300])

            detail = None
            if isinstance(resp, dict) and resp.get("record_id"):
                self._sleep(self.rnd.uniform(2.5, 6.0))
                detail = client.record_detail(resp["record_id"])
                if isinstance(detail, dict):
                    self.log(f"[8] recordDetail: status={detail.get('record_status')} "
                             f"reason={detail.get('record_failed_reason')!r} "
                             f"dist={detail.get('distance')}")
            if isinstance(resp, dict):
                stt = (detail.get("record_status")
                       if isinstance(detail, dict)
                       and detail.get("record_status") is not None
                       else resp.get("status"))
                rsn = (detail.get("record_failed_reason")
                       if isinstance(detail, dict) else None) \
                    or resp.get("record_failed_reason") or ""
                summary = {
                    "record_id": resp.get("record_id"),
                    "distance": resp.get("distance") or (detail or {}).get("distance"),
                    "used_time": resp.get("time") or resp.get("used_time"),
                    "record_status": stt,
                    "uid": resp.get("uid"),
                    "member": self.slot.label,
                    "game_id": resp.get("game_id") or rule["zone"],
                    "record_failed_reason": rsn,
                    "style": kind,
                    "zone_name": rule["zone_name"],
                }
                self.log("RUN_RESULT:" + json.dumps(summary, ensure_ascii=False))
                if summary["record_id"] and str(stt) in ("0", "1") \
                        and _positive_reason(rsn):
                    self.slot.after_submission(client)
                    return summary, 0
                if summary["record_id"] and not _positive_reason(rsn):
                    self.log(f"[WARN] 记录已创建但被判无效: {rsn}")
                    self.slot.after_submission(client)
                    return summary, 5
            return (resp if isinstance(resp, dict) else None), 4
        except ApiError as e:
            self.log(f"[FATAL] ApiError({e.code}) {str(e.msg)[:200]}")
            if e.code == 101:
                self.slot.mark_dead()
            return None, (9 if e.code == 9 else
                          (2 if e.code == 101 else
                           (3 if e.code == 3 else 4)))
        except OSError as e:
            self.log(f"[FATAL] 业务链不通 {e.__class__.__name__}: {e}")
            return None, 4
