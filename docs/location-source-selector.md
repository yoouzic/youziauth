# Location source selector — 1.4.2

## What changed

The 定位来源 control existed but was unreachable in practice: it sat at the bottom of the
自动打卡 schedule form, below the fold in a 1024×768 window, with no way to apply it except
saving the whole schedule form. The packaged `desktop_ui` was byte-identical to the source,
so the control was present in the installed build — it simply was not visible.

It is now a first-class part of the 寝室打卡 page sidebar:

- The 打卡定位 card (renamed from 定位来源与登录, since login now lives in its own page-level
  card next to 打卡账号) carries the selector, its own 保存定位来源 button, the current
  source badge and the scope hint.
- The 总览 page shows 打卡定位来源 in 后台安排, so the active source is visible without
  opening the dorm page.
- Switching the source no longer rewrites the schedule, and saving the schedule no longer
  rewrites the location source.

## Behaviour contract

| Item | Value |
| --- | --- |
| Sources | `windows` (真实定位 Windows / Wi-Fi) and `simulation` (模拟定位) |
| Scope | Global: location check, manual submission, automatic check-in |
| Persistence | `Settings.location_source` in the dorm store; survives restart |
| Bridge action | `location_source_save` with `{location_source}`; keeps every other saved field |
| Refused while | a check-in or a location probe is running, or the snapshot is not yet synced |
| School-facing label | unchanged: an accepted position still reports `provider: windows` |
| Simulation sample | `<dorm store>/location-sample.json`; missing or invalid sample reports `sample_missing` / `sample_invalid` |

Simulated positions are drifted around the saved sample (bounded radius, 3–25 m, and ±15 %
accuracy inside the 200 m acceptance limit) so a replayed fix does not repeat itself exactly.
The choice is local only and is never sent to the school.

## Wording kept deliberately short

Three verbose paragraphs were removed at the user's request: the simulation branch of the
mode hint (随机偏移 / 无需 Windows 定位授权), the whole 提交时会向学校发送… paragraph
(`location-submit-hint`), and the standalone 自动执行需电脑开机… line. The remaining
mode hint is only rendered for real location (`hidden` in simulation mode); the selector
label, scope line, current-source badge and the detection box already carry the needed
information. The card border accent was dropped as well.

## Validation

- 270 Python tests pass, including four new bridge tests: the source save keeps the saved
  schedule, rejects unknown sources, is refused during a probe, and the preview bridge
  switches without touching the schedule.
- 33 frontend tests pass, including: saving the source sends only `location_source`; saving
  the schedule never rewrites the source; the save button stays idle until the draft differs;
  a draft survives refresh, failed saves and concurrent edits; the mode hint is hidden in
  simulation mode; and the removed paragraphs stay out of the markup.
- Browser verification against the isolated preview server
  (`campus_auth_desktop.py --preview-server`) on a 1024×768 viewport:
  - the selector, save button and current-source badge are all inside the first screen
    (select y=448, button y=530, badge y=585 of 768);
  - picking 模拟定位 enables the save button within a render tick, saving switches the badge
    to 当前来源 · 模拟定位（非实时）, the overview row to 模拟定位 and the
    detection button to 检测模拟定位;
  - the hint becomes 模拟定位使用本机已保存样本，并非当前位置。

Screenshots: `build/qa/location-source-ui/dorm-v3.png` (before switching) and
`dorm-v3-saved.png` (after switching).

## Reproduction

```powershell
.\.tools\desktop-python\Scripts\python.exe campus_auth_desktop.py --preview-server --port 8801
# open http://127.0.0.1:8801/?preview=1#dorm
.\.tools\desktop-python\Scripts\python.exe -m unittest discover -s tests
node --test tests/test_desktop_ui.cjs
```
