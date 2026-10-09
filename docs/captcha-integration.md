# 统一认证（IDM）静默登录集成说明

> 把验证码识别模型接入 youziauth，让寝室打卡登录无需人工输入验证码。
> 模型训练与评估细节见 `data/captcha/PROGRESS.md`。

## 一、效果与数据

| 项目 | 数值 |
|---|---|
| 单次整串识别准确率（无偏交叉验证，自然分布） | **99.89%** |
| 单次整串识别准确率（保守下界，含刻意挑出的最难样本） | 99.48% |
| 单次延迟 | 约 **1.3 ms**（CPU 单核） |
| 模型体积 | **578 KB**（15.9 万参数，纯 NumPy，无深度学习框架） |
| 内存增量 | 约 7.6 MB（模型相关）；若计新增 numpy/Pillow 依赖约 25 MB |

### 连续尝试的成功率（每次都用**新验证码**）

| 尝试次数 | 至少一次成功 | 全部失败（转人工） |
|---|---|---|
| 2 次（当前实现） | 99.99% | **0.009%** |
| 3 次 | 99.9999% | 0.0000% |

按每天 4 次登录估算，2 次策略下约 **每 3 个月**才可能出现一次需要人工接管 —— 
实际收益与 3 次几乎相同，但最坏只消耗 **2 次**登录尝试。

> ⚠️ **为什么不做 3 次**：统一认证普遍有失败计数与锁定策略（常见 5 次/15 分钟）。
> 为省一次人工而把账号推到锁定边缘不划算，尤其是在其他程序共用同一账号时。

## 二、如何启用

1. 打开程序，左侧进入 **寝室打卡**；
2. 页面顶部「打卡账号」下面找到 **学校登录** 卡片，点 **管理静默登录** 展开；
3. 填写学号与密码，点 **保存凭据**（DPAPI 加密后仅存本机）；
4. 之后点同一张卡片上的 **学校登录 / 重新登录** 即会：
   自动填账号密码 → 自动识别验证码 → 自动提交。

**这是账号级设置，不是全局设置**：凭据存在**当前打卡账号自己的档案目录**里。
切换「打卡账号」时这张卡会跟着换成那个账号的凭据和登录态，两个账号的学号密码绝不串用
（见 `dorm_accounts.idm_store()` 的注释）。所以卡片上那句「仅属于『某某』」不是装饰。

**不填也能用**：不保存凭据时，行为与以前完全一致（人工在浏览器窗口输入）。

清除凭据：展开区最下方的 **清除凭据**，或手动删除当前账号档案下的
`%LOCALAPPDATA%\youziauth\accounts\<账号档案>\idm\credential.dat`。
（旧版单账号布局才是 `%LOCALAPPDATA%\youziauth\idm\credential.dat`，首次升级时会整体搬进第一个账号。）

**展开区里并排的两个「清除」不是一回事**，点错代价不同：

| 操作 | 清掉什么 | 代价 |
|---|---|---|
| **退出登录** | 本机的学校登录态 | 下次打卡需重新登录，并会**同时暂停自动打卡** |
| **清除凭据** | 保存的学号密码 | 下次登录需人工输入；当前登录态不受影响 |

> ⚠️ **集成时踩过的坑（重要）**：本项目有**两套界面** ——
> `dorm_panel.py`（旧版 Tkinter）与 `desktop_ui/`（当前实际使用的 pywebview 界面）。
> 最初只把入口加在了 Tkinter 面板里，导致在真实界面上**根本看不到**。
> 现已两处都接入；若日后改界面，请先确认当前实际运行的是哪一套。

| 界面 | 文件 | 凭据入口 |
|---|---|---|
| **Web UI（当前使用）** | `desktop_ui/index.html` + `app.js` + `desktop_bridge.py` | 「学校登录」卡片 → **管理静默登录** 展开区 |
| 旧版 Tkinter | `dorm_panel.py` | 「统一认证静默登录（可选）」区域 |

### 2.1 Web UI 里它为什么在这个位置

这张卡**换过位置**，别照旧文档去找：它原先只是塞在右侧栏「定位来源与登录」下面的一张
独立卡片，要滚动才看得到；而「清除学校登录凭据」留在定位卡里，两个作用不同的「清除」
分居两卡、都没有解释。现在的信息架构是：

- **「学校登录」是页面级卡片**，紧贴「打卡账号」，让「账号 → 登录 → 打卡」连成一条线；
  卡上给出登录态徽标、账号归属和凭据状态，操作按钮就在右边。
- **凭据表单收进 `管理静默登录` 折叠区**，默认收起；展开状态由用户控制，
  `render()` 每 1.5 秒重绘也不会把它关掉。
- **右侧栏只剩「打卡定位」**，卡名不再叫「定位来源与登录」（登录已经不在那里了）。
- 登录态来自快照的 `dorm.has_session`，它只读「会话密文文件在不在」、**不解密令牌**
  （`Store.has_session()`）。这条边界由 `test_snapshot_excludes_secrets` 守着，
  改这里之前先读那条测试。

## 三、实现结构

| 文件 | 职责 |
|---|---|
| `captcha_ocr.py` | 模型加载与推理（纯 NumPy 前向），含**低置信拒答** |
| `idm_credentials.py` | IDM 学号/密码的 DPAPI 加密存储（用户范围） |
| `idm_http.py` | **纯 HTTP 客户端提交登录**（绕开站点 WAF 对浏览器 POST 的拦截） |
| `idm_login.py` | IDM 登录页判定与重试策略：导出表单字段 → 交给 `idm_http` 提交 |
| `dorm_login.py` | 集成点：`enter_idm_login()` 点「统一认证登录」推进到 IDM 表单页；成功后把 HTTP 会话 cookie 灌回浏览器走完 SSO；有凭据才静默登录，失败静默回退人工 |
| `dorm_panel.py` | 凭据录入界面与状态显示 |
| `data/captcha/model.npz` | 模型权重（随包分发） |

### ★ 登录页的真实地址链（这一段曾经搞错，务必先看）

```
of.swu.edu.cn/cas/login?service=...
  → uaaap.swu.edu.cn/cas/login?service=...    「推荐登录」三选一选择页
  → 点「统一认证登录」                          onclick = _goLogin()
  → idm.swu.edu.cn/am/UI/Login?realm=...      账号密码表单页（4 位验证码在这里）
```

两个致命细节：

1. **「统一认证登录」必须“点击”，不能“重新导航”。**
   该按钮的 `onclick` 是 `_goLogin()`，给当前 URL 追加 `federalEnable=true` 后
   由**浏览器自身**发起同源导航。用 `page.goto` 重新导航同一个 URL（原样或带上
   `federalEnable=true`）会被站点的动态防护判为异常并返回 **HTTP 400**，
   于是登录直接失败、并在自动打卡里被反复重试 —— 这正是「连续登录」的根因。
2. **按钮上的文字画在图片里**：`img/unified_button.png`。DOM 中搜「统一认证登录」
   命中 **0 次**，只能用 `div[onclick*="_goLogin"]` 定位。

> 只执行 `open_login_page` 时，落点是上面的**选择页**，不是表单页。
> 早先在这个选择页上探测 `#loginName` 等元素，自然全部 `visible=False`，
> 于是被误判成「站点改版 / 验证码换成滑块了」。

### ★ 不要用 Playwright 的 launch() 去探测这个站点

站点启用了瑞数信息动态防护（页面里可见 `$_ts.cd` / `$_ts.nsd` 与随机名 Cookie）。

| 访问方式 | 结果 |
|---|---|
| `dorm_login.owned_browser`（普通 Chrome + CDP，**程序的实际用法**） | 全部 **200**，流程通畅 |
| `playwright.chromium.launch(executable_path=...)`（探针常用写法） | 大量 **400 / 412**（子资源、`federalEnable` 跳转、`/cas/verCode` 都中招） |
| `urllib`（无浏览器指纹） | 全部 **200** |

同一 URL 反复请求，状态码**完全稳定**（各 15 次均 200）—— 所以那些 400
**不是**服务端故障，而是自动化指纹触发的**确定性**拦截。

> 排查这个站点**必须**用 `owned_browser`。用 `launch()` 会得到完全错误的结论
> —— 我就据此误删过必要的一步，把「连续登录」改得更糟。

### ★★ 最关键的根因：**脚本**发起的浏览器 POST 到不了服务端

这是「登录成功却卡住不跳转」的真正原因，也是整件事里最难查的一环：

> `idm.swu.edu.cn` 前面有一层**瑞数（RiverSecurity）类动态 WAF**，它把
> **脚本驱动的浏览器内「带 body 的 POST 到 `/am/`」拦成 HTTP 400**，
> 响应体只有 `\r\n\r\n\r\n` —— **就是一个空白页**。

被拦的包括页面自己的验证码预校验 `POST /am/validatecode/verify.do`，
以及登录表单的正式提交 `POST /am/UI/Login`（`form.submit()` 与 `fetch` 一样）。

> **2026-09-26 精度修正（重要）**：实测（`tools/captcha/probe_browser_submit.py`）
> 被拦的是**脚本**发起的提交 —— 哪怕用「拟人逐字输入 + 真实点击 `#button`」
> （照上游 `login_once()` 的手法、连页面自己附加的动态令牌 `?ZUY2FAwZ=...` 都带上了），
> 预校验 `verify.do` 仍是 **400**，页面据此把本地状态置错、弹出「验证码输入不正确」，
> **点击登录根本不发提交请求**（报告：`.tools/browser_submit_report.txt`）。
> 而**真人用键盘输入**不受此限：上游 `dan-cun/swu-daka` 的默认「人工手动登录」
> 模式靠的就是这一点（同一套 CDP 启动的 Chrome 窗口）。
> 所以：脚本路径必须绕行；人工路径是可靠的后备 —— 两者分工见下文。

**症状**：填完账号密码与验证码、点了登录之后，浏览器停在一个**空白的
`idm.swu.edu.cn/am/UI/Login`**，既不跳转也不报错；自动化脚本据此判失败并
反复重试，表现为「连续登录」。**验证码识别其实完全正确** ——
问题在于这个 POST 压根没到服务器。

**绕行办法：把提交交给纯 HTTP 客户端。** 它不被这层 WAF 拦截：

| 请求（由 urllib 发出） | 结果 |
|---|---|
| `GET  /am/validate.code` | 200 `image/jpeg`（100×30，本地模型可读） |
| `POST /am/UI/Login` | 200/302，响应头 `X-AuthErrorCode` 给出真实鉴权结果 |

> **★ 但 HTTP 客户端绝不能带「页面 JS 读得到的 cookie」（2026-09-26 实测）**
>
> 站点动态防护把自己要读的 cookie **必须让页面 JS 能读到**（会出现在 `document.cookie`
> 里），真正的服务端会话 cookie 则是 HttpOnly 的。于是：
> **一个不是由那个页面 JS 发起、却带着这些 JS 可见 cookie 的请求，会被判成重放，
> 一律回 HTTP 400 + 空白页（`\r\n\r\n\r\n`）** —— 换 User-Agent 也无效。实测：

| urllib 带的 cookie | 结果 |
|---|---|
| 浏览器全部 cookie | 400（6 字节空白；换 Chrome/131、Chrome/153、页面真实 UA 都一样） |
| 只带 HttpOnly 会话 cookie（`61zq…O`） | **200**，且 POST 真到达服务端：`X-AuthErrorCode=-1` +「UAMS 验证失败。用户名或密码错误。」 |
| 不带 cookie | 200 |

> 这正是 2026-09-26 那次「取验证码就 400（`captcha_http_400`）、认证成功后卡在
> `idm.swu.edu.cn/am/oauth2/authorize` 一动不动」的根因之一。修法见
> `idm_http.browser_cookies(context, page)`：传了 `page` 就按 `document.cookie`
> 把 JS 可见的那几枚剔掉，只带 HttpOnly 的会话 cookie。
> 复核脚本：`tools\probe_submit_subsets.py`（假账号逐组提交，报告
> `.tools/submit_subsets_report.txt`）。

另外一件同样重要的事：**`login()` 不能把上一次登录保存的旧学校 cookie 灌进新浏览器**
—— 新旧两代 cookie 混在一起时，连**浏览器自己**的请求（认证后的授权跳转）也会被 400。
现在旧会话接不上就 `clear_cookies()` 清干净再用干净会话重走一遍。

第三件（2026-09-26 15:27 实机抓到）：**把 HTTP 会话灌回浏览器前必须先清浏览器自己的 cookie**
（`dorm_login.adopt_idm_cookies` 现在先 `clear_cookies()` 再 `add_cookies`）。
直接灌会与浏览器原有的主机域 cookie **同名并存**（我们统一设 `.swu.edu.cn`），
动态防护看到两代同名 cookie 就判重放 → 认证之后那一跳
`idm.swu.edu.cn/am/oauth2/authorize` 回 400 → 日志里出现
「认证通过（…X-AuthErrorCode=0）」却拿不到 token。

于是职责拆开：**浏览器**只走 SSO 链路、持有会话、导出表单字段；
**`idm_http.py`** 负责取验证码与提交账号密码。成功后把 HTTP 会话的 cookie
灌回浏览器（域统一设 `.swu.edu.cn`），浏览器再走一遍 SSO 收尾，
由 `exchange-token` 响应头拿到 `fighter-auth-token`。

**交回人工时必须刷新页面**：失败的那个 POST 会让会话失效，而我们取过的每一张验证码
都会让页面上显示的那张作废（服务端只认最后取的一张）—— 把人留在这种页面上，
他照着图输入也必然失败。交互模式下自动尝试**明确失败**后，程序会重走一次链路
刷出一张新表单（新会话 + 当前验证码）再交给用户，之后不再碰这个页面。
唯一的例外：如果检测到**用户已经在这个窗口里开始输入**（账号/密码/验证码框非空），
就不刷新 —— 宁可让他自己点一下验证码图换一张，也不能把他正在输入的内容冲掉。

三条必须遵守的实测结论：

1. **账号密码明文提交即可。** 页面那套 `strEnc` 加密是历史包袱；
   用它提交**反而会被服务端判「用户名或密码错误」**，让人误以为密码错了。
2. **表单隐藏字段必须实时从页面导出**（`goto` / `SunQueryParamsString` /
   `encoded` 等），不能硬编码 —— 它们随会话变化。
3. **提交失败后会话即失效，同一会话内重试没有意义**，必须重走 SSO 换新会话；
   而**直接 reload 登录页会被 WAF 打成 400 空白页**（原先的
   `_reload_fresh_session` 干的正是这件被禁的事，已删除）。

### 成功判据与失败原因分类（判定逻辑的修正）

成功判据从「是否离开 idm 主机」这个**间接推断**换成了服务端明示的响应头
**`X-AuthErrorCode == "0"`**，可靠得多。

更重要的是：`X-AuthErrorCode` 对下面两种失败**都是 `-1`**，光看错误码分不出来，
但**正文文案不同**（实测抓取）：

| 情形 | X-AuthErrorCode | 服务端文案 |
|---|---|---|
| 验证码错（提交 `0000`） | `-1` | `UAMS 验证失败。动态口令验证失败` |
| 验证码对、账号密码错 | `-1` | `UAMS 验证失败。用户名或密码错误。` |

> 早先文档里写「两者提示完全相同、无法区分」是**错的** —— 那次比的是
> 「验证码错误」与「验证码为空」，两者都是验证码问题，当然一样。

据此重试策略改为：

* **文案含「用户名或密码错误」→ 立即停止**，不再消耗第 2 次登录尝试。
  密码错时重试必然再错，只会白白逼近账号锁定阈值；同时把
  「请在面板中更新已保存的凭据」直接报给用户，而不是含糊的"登录等待超时"。
* **文案是「动态口令验证失败」→ 值得重试**，重走 SSO 换新会话再试一次。

### ★ 判据必须分层，不能只看一个响应头（2026-09-26 修正）

上面那条「`X-AuthErrorCode == "0"`」单判据 + 「非密码错就换会话重试」的组合，
在**判据缺失**时会犯一个代价很大的错：一个**服务端已经认证成功**的登录会被判成失败，
于是正在走的登录页被 `fresh_session()` 强行导航走、紧接着又发一次登录 ——
用户看到的就是「第一次登录成功、学校登录页被跳走、程序又登了一次」，
而原本那条跳转链（POST 的 302 → oauth2/authorize → 联邦 → exchange-token）被打断。

现在按**证据强弱**分层（`idm_http.judge_response`），并把「说不清」与「明确失败」分开：

| 证据 | 判定 | 之后做什么 |
|---|---|---|
| `X-AuthErrorCode == "0"` | 通过（判据 `header`） | 灌 cookie → 顺着下一跳走完 SSO |
| 3xx 且落点 == 表单自带的 `goto`（页面声明的成功落点） | 通过（判据 `redirect`） | 同上 |
| 3xx 但落回登录页 / `gotoOnFail` / 非学校域名 | 明确失败（`rejected`） | 停手，交回人工；**不重登** |
| 文案含「用户名或密码错误」 | 明确失败（`credentials`） | 停手，提示更新凭据 |
| 文案是「动态口令验证失败」 | 明确失败（`captcha`） | 换新会话再试一次（唯一值得的重试） |
| 响应丢失 / 无文案的 4xx-5xx（含 WAF 400 空白页）/ 落点不认识 | **未确认**（`unconfirmed`） | 先接上会话、走完跳转链等 token 定论；**绝不自动重登** |

另外两条：

* **POST 发出去了 ≠ 没提交。** 之前把「POST 抛异常（超时/连接重置）」也算成
  「未提交、可以原地重试」，同样会踩掉一次可能已经成功的登录；现在它是
  `sent=True, failure_kind="unconfirmed"`。
* **认证成功后顺着服务端给的下一跳走**（`Location`，`dorm_login.resume_target` 只放行
  学校域名、http 同主机升级为 https），而不是从 CAS 初始页重走一遍 —— 后者就是
  「打断原先跳转」本身；接不上才回退整条链路。
* **交换成功后的身份核验抖动不算登录失败**：`exchange-token` 成功即说明登录成功，
  随后的 `api.user` 只是确认「你是谁」，transient 错误先重试，仍不行也保住会话
  （`dorm_login.keep_session`），不把刚拿到的 token/cookie 丢掉。

诊断：每一步只记受控字段（第几次、验证码与置信度、HTTP 状态、`X-AuthErrorCode`、
判定与决策）到 `<LOCALAPPDATA>\youziauth\dorm\login.log`，绝不含密码、token 或响应原文。

### 验证码接口的真实行为

`/am/validate.code`（相对 `idm.swu.edu.cn`，返回 **100×30 JPEG 的 4 位数字图**）
**每次请求都会生成一张新验证码**：同一会话连续请求 3 次均 200，且三张图各不相同。

因此：**取图与提交必须用同一个 HTTP 会话**（脚本里就是这么做的）——
服务端校验的就是你刚取到的那张图。只有在「识别置信度不足、根本没发出 POST」
时才可以在同会话内换一张重试；一旦 POST 发出且未通过，会话即失效，必须重走 SSO。

### 登录流程

```
打开浏览器 → 导航到 CAS 选择页
   ├─ 已保存凭据？
   │    ├─ 是 → 点「统一认证登录」进入 IDM 表单页
   │    │        （无凭据时**不点**：这是三选一的选择页，选择权留给用户）
   │    │        → 从页面导出 forms['Login'] 的全部隐藏字段 + 当前会话 cookie
   │    │        → 纯 HTTP 客户端：GET 验证码 → 模型识别（conf≥0.9）
   │    │        → 纯 HTTP 客户端：POST /am/UI/Login（明文账号密码）
   │    │        → 判定（分层，见上表）：
   │    │             通过（header / redirect）
   │    │                   → 灌 cookie → **顺着服务端给的下一跳**走完 SSO → 捕获 token
   │    │             未确认（响应丢失 / 判据缺失 / 落点不认识）
   │    │                   → 灌 cookie → 走完链路等 token；**不再自动重登**
   │    │             明确失败：密码错 → 立即停止并提示改凭据
   │    │                       验证码错 → 换新会话再试一次（最多 2 次）
   │    │                       被退回 / 其它 → 停手，交回人工
   │    └─ 否 → 停在选择页，等待人工操作（原行为）
   └─ 任意环节异常 → 静默回退到原人工流程
```

## 四、已知限制（务必知悉）

1. ~~无法区分失败原因~~ —— **已解决**。改走纯 HTTP 提交后能读到响应正文，
   而「用户名或密码错误」与「动态口令验证失败」文案不同、可以区分
   （只是 `X-AuthErrorCode` 恰好都是 `-1`）。密码错时**立即停止**，
   不再浪费第 2 次尝试。详见上文「成功判据与失败原因分类」。
2. **高置信错误存在**。实测错误样本中有 conf=0.997 却识别错误的情况，
   失效模式集中在「蓝色弧线穿过首位时把 3/0/6/1/8 误读为 5/9/2/0」。
   置信度闸门只能拦掉最难的一批（conf<0.9，约 4%），无法完全避免。
3. **重复识别同一张图无效**。模型是确定性函数（实测 5 次预测逐位相同、概率差异为 0），
   只有**重新获取一张新验证码**才构成新的独立机会。
4. **低置信时不猜**。conf<0.9 直接转人工而非继续猜 —— 猜错只会浪费登录尝试。
5. **脚本发起的浏览器提交注定失败；真人手动登录可行**（2026-09-26 实测修正）。
   被 WAF 拦的是**脚本**路径：`tools/captcha/probe_browser_submit.py` 用「拟人逐字输入
   （110–260ms/字符）+ 截 `#kaptchaImage` + 真实点击 `#button`」（照上游 `login_once()`，
   假账号）实测，页面自己的预校验 `POST /am/validatecode/verify.do`（已带动态令牌）
   仍被判 **HTTP 400**，页面因此把本地状态置错、弹「验证码输入不正确」，
   **提交 POST 一次都没发出**（报告 `.tools/browser_submit_report.txt`）。
   所以脚本填表这条路是死的，纯 HTTP 客户端绕行仍然必要；
   但**真人在同一窗口里用键盘输入是可行的**——上游 `dan-cun/swu-daka` 的默认
   「人工手动登录」模式正是靠这一点稳定工作。程序据此在交互模式下把失败后的页面
   刷新成新表单再交回人工（见上文「交回人工时必须刷新页面」）。
6. **依赖站点行为，改版可能失效**。若 WAF 策略变化、验证码类型改变，
   或 `X-AuthErrorCode` / 文案措辞调整，都需要重新探测。
   探测手段：`python tools\captcha\probe_auth_error_codes.py`（比对失败文案）。
   分层判据里的 `redirect` 一支依赖表单自带的 `goto` / `gotoOnFail` 字段
   （页面声明的成功/失败落点，随会话变化）—— 站点若改掉这两个字段，
   该支会退化为「未确认」，**不会**误判成失败，最坏只是回到人工流程。
7. **诊断日志不是审计日志**。`login.log` 只保留最近 200 行受控字段，
   且不写密码/token/响应原文；排查完可随时删除，不影响任何状态。
8. **真实账号端到端尚未验证**。上述机制已用**假账号**在真实环境中逐项验证 ——
   两次 POST 都真正到达服务端并拿回真实 `X-AuthErrorCode=-1` 与
   「用户名或密码错误」文案（修复前只会是空白页，连错误码都拿不到），
   换会话后验证码确实变成另一张。但「真实账号能否一路登录成功」需要你的凭据才能确认。
   可自行重跑：
   - `python tools\captcha\diag_login_page.py` —— 看登录页结构与识别结果（有头，不提交凭据）；
   - `python tools\captcha\verify_silent_flow.py` —— 全链路含重试（用假账号提交）。

## 五、构建与打包

- `requirements-build.txt` 新增 `numpy==2.4.4`（精确固定，测试会校验）；
- `packaging/youziauth.spec` 的 `datas` 增加 `data/captcha/model.npz`，
  `hiddenimports` 增加 `captcha_ocr` / `idm_login` / `idm_credentials`；
- **模型只打进 GUI 应用**：后台 agent（`youziauth-agent`）仅做校园网 ePortal 认证，
  不涉及验证码，保持精简；
- `build_msi.ps1` 增加构建期前置校验：模型文件缺失或 numpy 不可用时**直接构建失败**，
  避免问题拖到运行时才暴露。

## 六、自测

```powershell
$env:PYTHONIOENCODING="utf-8"

# 登录页结构与识别自检（有头，不提交凭据）
python tools\captcha\diag_login_page.py

# 浏览器原生提交到底通不通（拟人输入 + 真实点击；假账号，会弹一个窗口）
python tools\captcha\probe_browser_submit.py

# 静默登录全链路（含点按钮、取图、识别、提交、换会话重试；用假账号提交）
python tools\captcha\verify_silent_flow.py

# 集成自测：识别 / 凭据加密 / 无凭据回退 / 打包路径
python tools\captcha\selftest_integration.py

# 验证独立推理实现与训练侧完全一致（防止两份实现漂移）
python tools\captcha\verify_ocr_impl.py

# 全项目测试
python -m pytest tests -q
```

## 七、打包与安装验证

### 构建

```powershell
.\build_msi.ps1
```

产物：`dist\youziauth.msi` —— **1.6.2**，68.1 MB，SHA-256
`8A6674347A7F6B08F73E4AB4E1F78618BA9B15E1C604290F617CC58A9886AC70`
（含模型与 numpy，比不含这些的 54.9 MB 大约多 13 MB）

> 版本号必须递增：MSI 靠 `MajorUpgrade` 升级，同版本号会**并存安装**而不是替换。
> 1.6.0 / 1.6.1 都已发布过，所以本次为 1.6.2。
>
> 写 `VERSION` 时注意：Windows PowerShell 5.1 的 `-Encoding utf8` 会写入 BOM，
> 导致 `int()` 解析失败（`invalid literal for int() with base 10: '\ufeff1'`）。
> 用 `-Encoding ascii`。

**构建期修复**：`build_msi.ps1` 顶部设了 `$ErrorActionPreference = "Stop"`，
而 PyInstaller 在**成功**时也会往 stderr 写 INFO 日志，PowerShell 会把这类正常输出
当成致命错误中断构建（实测踩到）。已改为：调用 PyInstaller 期间临时
`$ErrorActionPreference = "Continue"`，仅依据退出码判断成败。

### 打包内容核对

| 检查项 | 结果 |
|---|---|
| 模型文件 | ✓ `_internal\data\captcha\model.npz`（591,741 字节） |
| numpy | ✓ `numpy` / `numpy-2.4.4.dist-info` / `numpy.libs` |
| 界面文案 | ✓ `_internal\desktop_ui\index.html` 已含新版说明 |
| 主程序 | ✓ `youziauth.exe` / `youziauth-agent.exe` |
| WiX 清单 | ✓ 覆盖 1759 个文件 |

### ★ 打包后真实验证（本轮已完成，这是最强证据）

程序内置自检开关 `--dorm-login-probe`，**在冻结环境里真的走一遍登录流程**
（不输凭据、不提交）：

```powershell
$out = Join-Path $PWD '.tools\dorm_probe_packaged.json'
Start-Process -FilePath (Join-Path $PWD 'dist\youziauth\youziauth.exe') `
  -ArgumentList '--dorm-login-probe', $out -Wait -PassThru
Get-Content $out -Raw
```

> ⚠️ `youziauth.exe` 是 GUI 子系统程序，直接 `& .\youziauth.exe` 时 PowerShell
> **不会等它**，会立刻返回并报"报告不存在"。必须用 `Start-Process -Wait`。

实测结果（打包产物，1.6.2）：

```json
{
  "ok": true,
  "dpapi": true,
  "tk": true,
  "school_login_page": true,
  "captcha_image": true,
  "captcha_model": { "digits": 4, "confidence": 0.9996 },
  "http_client": { "status": 200, "bytes": 2015, "form_fields": 9, "cookies": 4 },
  "playwright": true
}
```

意义：**发布出去的二进制**确实能
（1）经 CAS 选择页点进 IDM 表单页、
（2）加载 100×30 验证码图、用包内模型读出 4 位数字、
（3）**纯 HTTP 客户端在冻结环境里成功取到验证码并导出全部 9 个表单字段**
（`http_client` 那一项）—— 这正是原先被 WAF 打断、导致登录卡死的那一环。
（该自检只取图、**不提交任何凭据**。）

> 该自检脚本 `dorm_selftest.py` 原先只 `open_login_page` 就等 `#loginName`，
> 在「必须点按钮」的流程下必然超时 —— 已同步修正为调用 `enter_idm_login()`。

> ⚠️ 安装 MSI 需要管理员权限（UAC 确认）；若程序已在运行（托盘/后台 agent），
> 升级前建议先退出，安装后重新启动。

