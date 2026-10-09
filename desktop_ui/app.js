'use strict';
const $ = id => document.getElementById(id);
const titles = {overview:'今日概览',network:'校园网',dorm:'寝室打卡',records:'运行记录',updates:'软件更新'};
let state = null, logType = 'network', pending = false, refreshTask = null, epoch = 0, syncedEpoch = -1, timer;
const dirty = {network:false, dorm:false};
const revisions = {network:0, dorm:0};
let sourceDirty = false, sourceRevision = 0;
let api;
let mapState = null;
let mapDrag = null, mapSuppressPick = false, mapNameFor = null, accountNameFor = null;
const MAP_TILE = 256, MAP_MIN_ZOOM = 13, MAP_MAX_ZOOM = 19;
const MAP_TILE_TRIES = 3, MAP_TILE_BACKOFF = [1200, 4000];  // ms before each retry of one tile
const MAP_TILE_STALL = 8000;   // ms with nothing drawn before the next provider takes the frame

function notify(message, error=false) {
  clearTimeout(timer);
  $('toast').textContent = message;
  $('toast').classList.toggle('error', error);
  $('toast').hidden = false;
  timer = setTimeout(() => {$('toast').hidden = true;}, error ? 7000 : 4200);
}
function navigate() {
  const page = location.hash.slice(1) in titles ? location.hash.slice(1) : 'overview';
  for (const id of Object.keys(titles)) $(id).hidden = id !== page;
  document.querySelectorAll('.nav-link').forEach(link => {
    const active = link.hash === '#' + page;
    link.classList.toggle('active', active);
    if(active) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current');
  });
  $('page-context').textContent = titles[page];
  window.scrollTo(0,0);
}
window.addEventListener('hashchange', navigate);
window.desktopNavigate = page => { if (page in titles) location.hash = page; };
navigate();
$('date-label').textContent = new Intl.DateTimeFormat('zh-CN',{month:'long',day:'numeric',weekday:'long'}).format(new Date());

function badge(id, text, tone='') {$(id).textContent=text;$(id).className='badge'+(tone ? ' '+tone : '');}
function sourceSummary(simulation) {return simulation?'模拟定位':'真实定位（Windows / Wi-Fi）';}
function markDirty(kind, value) {
  dirty[kind] = value;
  $(kind+'-save-state').textContent = value ? '有未保存的修改' : '设置已同步';
  $(kind+'-save-state').classList.toggle('dirty',value);
}
['network','dorm'].forEach(kind=>$(kind+'-form').addEventListener('input',()=>{revisions[kind]++;markDirty(kind,true);}));

function savedLocationSource(){return state?.dorm.settings.location_source||'windows';}
function requireSavedLocationSource(){
  if(!state||syncedEpoch!==epoch){notify('定位设置尚未同步，请稍后重试。',true);return false;}
  if($('location-source').value===savedLocationSource())return true;
  notify('请先保存定位来源，再检测或提交。',true);
  location.hash='dorm';$('location-source').focus();
  return false;
}
function render() {
  if(!state) return;
  const n=state.network,d=state.dorm,simulation=savedLocationSource()==='simulation';
  $('preview-tag').hidden = !state.preview;
  $('preview-tag').textContent = state.location_diagnostic ? '真实定位诊断 · 其他功能为演示' : '演示预览';
  $('overview-location-source').textContent=sourceSummary(simulation);
  // Only the real-location mode needs instructions; the simulation wording is dropped.
  $('location-mode-hint').hidden=simulation;
  $('location-mode-hint').textContent='实时位置由 Windows 决定来源，不保证只使用 Wi-Fi。如遇系统权限提示，请选择允许；已拒绝的权限需在 Windows 定位设置中开启。';
  const locationState=state.location||{state:'idle',message:simulation?'点击按钮检测已保存的模拟位置，不会提交打卡。':'点击按钮授权并检测定位，不会提交打卡。',busy:false};
  $('location-message').textContent=(simulation&&locationState.state==='ready'?'模拟定位（非实时）· ':'')+locationState.message;
  $('location-message').classList.toggle('location-success',locationState.state==='ready');
  $('location-message').classList.toggle('location-error',!['idle','checking','ready'].includes(locationState.state));
  $('authorize-location').disabled=pending||syncedEpoch!==epoch||locationState.busy;
  $('authorize-location').textContent=simulation
    ? (locationState.busy?'正在检测模拟定位…':'检测模拟定位')
    : (locationState.busy?'正在授权 / 检测…':'授权并检测定位');
  $('location-checked').textContent=locationState.checked ? (simulation?'最近模拟检测 · ':'最近检测 · ')+locationState.checked : '仅检测定位，不会提交打卡';
  $('network-status').textContent=n.message;
  $('network-summary').textContent=n.message;
  $('checked-label').textContent=n.checked ? '最近检测 · '+n.checked : '尚未执行连接检测';
  const names={online:'网络已连接',offline:'需要重新连接',checking:'正在检测',stopped:'尚未检测',error:'需要检查设置'};
  $('network-headline').textContent=names[n.state] || '等待检测';
  badge('network-badge',n.state==='online'?'连接正常':n.state==='checking'?'检测中':n.state==='offline'?'未连接':n.state==='error'?'检测异常':'等待检测',n.state==='online'?'success':['offline','error'].includes(n.state)?'warning':'');
  const dormNames={signed:'今日已完成',ready:'今日待打卡',idle:'查看今日安排',logged_in:'已登录，待查询',login_required:'需要学校登录',uncertain:'结果待确认',location_required:'需要检查定位',expired:'任务已过期',waiting:'等待学校时段',no_task:'今天暂无任务',error:'需要检查',network_error:'网络异常',cancelled:'操作已取消',busy:'处理中'};
  const dormLabel=d.busy?'正在处理…':(dormNames[d.state]||'请查看任务状态');
  $('dorm-headline').textContent=dormLabel;
  $('dorm-summary').textContent=d.message;
  const tone=d.state==='signed'?'success':['uncertain','error','network_error','location_required','login_required'].includes(d.state)?'warning':'';
  badge('dorm-badge',dormLabel,tone);badge('task-badge',dormLabel,tone);
  $('monitor-summary').textContent=n.startup?'系统代理运行中':n.monitoring?'正在后台检测':'未开启';
  $('auto-summary').textContent=d.settings.enabled ? d.settings.start+'–'+d.settings.end : '未开启';
  badge('monitor-badge',n.startup?'系统代理管理':n.monitoring?'运行中':'未开启',n.monitoring?'success':'');
  $('agent-hint').textContent=n.startup?'由系统代理持续运行；停用请取消开机自启动并保存。':'开启前请先保存设置。';
  $('overview-attention').textContent=n.state!=='online'?'从一次连接检测开始':d.state==='signed'?'今天的安排，已妥当':d.state==='ready'?'别忘了今晚的寝室打卡':'看看今天的寝室安排';
  $('task-title').textContent=d.task?d.task.title:'先看看今天的安排';
  $('task-message').textContent=d.busy?'正在处理，请稍候；学校登录可能需要在浏览器中完成。':d.message;
  if(d.state==='location_required'&&locationState.state==='ready'&&!d.busy)$('task-message').textContent=(simulation?'模拟定位检测已通过（非实时）。':'定位检测已通过。')+'请查询今日任务，刷新任务状态后再提交。';
  $('task-details').hidden=!d.task;
  if(d.task){$('task-date').textContent=d.task.date;$('task-time').textContent=d.task.start+'–'+d.task.end;$('task-address').textContent=d.task.address||'以学校任务为准';}
  $('schedule').textContent=d.schedule;
  if(!dirty.network){$('username').value=n.username;$('interval').value=n.interval;$('startup').checked=n.startup;}
  if(!dirty.dorm){$('auto-enabled').checked=d.settings.enabled;$('auto-start').value=d.settings.start;$('auto-end').value=d.settings.end;$('auto-interval').value=d.settings.interval;}
  if(syncedEpoch===epoch&&!sourceDirty&&$('location-source').value!==savedLocationSource())$('location-source').value=savedLocationSource();
  $('save-location-source').disabled=pending||syncedEpoch!==epoch||$('location-source').value===savedLocationSource();
  $('password-hint').textContent=n.has_password?'已保存加密密码；留空保存即可保留。':'尚未保存密码，请填写后保存。';
  document.querySelectorAll('[data-action="network_check"]').forEach(b=>b.disabled=pending||n.busy||(!n.startup&&n.monitoring));
  document.querySelectorAll('[data-action="network_start"]').forEach(b=>b.disabled=pending||n.busy||n.monitoring);
  document.querySelectorAll('[data-action="network_stop"]').forEach(b=>b.disabled=pending||!n.monitoring||n.startup);
  document.querySelectorAll('[data-action="dorm_login"],[data-action="dorm_query"]').forEach(b=>b.disabled=pending||d.busy);
  $('submit-dorm').disabled=pending||syncedEpoch!==epoch||d.busy||!['ready','uncertain'].includes(d.state);
  $('submit-dorm').firstChild.textContent=d.state==='uncertain'?'回查提交结果 ':'提交今日打卡 ';
  $('cancel-dorm').disabled=pending||!(d.busy||state.accounts?.busy);   // 别的账号在忙时也要能取消
  $('logout-dorm').disabled=pending||d.busy;
  renderLogin(d,pending);
  $('idm-clear').disabled=!d.has_idm_credentials||pending||d.busy;
  $('idm-username').disabled=pending||d.busy;
  $('idm-password').disabled=pending||d.busy;
  renderAccounts();
  if(!simulation&&mapState)closeMap();
  $('open-map-picker').hidden=!simulation||!!mapState;
  document.querySelectorAll('button[type="submit"]').forEach(b=>b.disabled=pending||(b.closest('form').id==='dorm-form'&&d.busy));
  renderUpdates();
  renderLogs();
}
function renderLogs(){if(!state)return;const text=state.logs[logType]||'';$('log-output').textContent=text;$('log-output').hidden=!text.trim();$('empty-records').hidden=!!text.trim();}
function updateSize(bytes){
  if(bytes<1024)return bytes+' B';
  if(bytes<1048576)return (bytes/1024).toFixed(1)+' KiB';
  return (bytes/1048576).toFixed(1)+' MiB';
}
function renderUpdates(){
  const u=state.update,ready=u.state==='ready',error=u.state==='error';
  const names={idle:'等待检查',checking:'正在检查',downloading:'正在下载',verifying:'正在验证',ready:'可以安装',up_to_date:'无需更新',error:'更新未完成',launching:'正在打开安装向导',launched:'已打开安装向导'};
  badge('update-status',names[u.state],error?'error':ready||u.state==='up_to_date'?'success':'');
  $('update-current-version').textContent=u.current_version;
  $('update-latest-version').textContent=u.latest_version||'尚未检查';
  $('update-message').textContent=u.message;
  $('update-checked').textContent=u.checked?'最近检查 · '+u.checked:'尚未检查';
  $('check-updates').textContent=error?'重新检查':'检查更新';
  const blocked=pending||syncedEpoch!==epoch||u.busy;
  $('check-updates').disabled=blocked;
  $('install-update').hidden=!ready;
  $('install-update').disabled=blocked||!ready;
  const progressVisible=['downloading','verifying','ready','launching','launched'].includes(u.state);
  $('update-progress').hidden=!progressVisible;
  $('update-progress').value=u.progress;
  $('update-progress-label').textContent=progressVisible?'下载进度 · '+Math.round(u.progress)+'%':'下载进度';
  $('update-download-size').textContent=u.total_bytes?updateSize(u.downloaded_bytes)+' / '+updateSize(u.total_bytes)
    :u.downloaded_bytes||u.state==='downloading'?updateSize(u.downloaded_bytes)+' · 总大小未知':'尚未下载';
  const notice=ready?'新版本 '+u.latest_version+' 可以安装 · '+u.message+' 安装前需要你的确认。':error?'软件更新未完成 · '+u.message:'';
  // Keep polling quiet, including the live region when its text has not changed.
  if($('update-notice-text').textContent!==notice)$('update-notice-text').textContent=notice;
  $('update-notice').classList.toggle('error',error);
  $('update-notice').hidden=!ready&&!error;
}
function requireUpdateSync(){
  if(!state||syncedEpoch!==epoch){notify('更新状态尚未同步，请稍后重试。',true);return false;}
  if(pending){notify('其他操作正在处理，请稍后重试。',true);return false;}
  return true;
}
function requireReadyUpdate(){
  if(!requireUpdateSync())return false;
  if(state.update.state!=='ready'||state.update.busy){notify('更新状态已变化，请在下载与验证完成后重新确认。',true);return false;}
  if(dirty.network||dirty.dorm||sourceDirty||$('location-source').value!==savedLocationSource()){
    notify('有未保存的校园网、寝室打卡或定位来源设置，请先保存，再安装更新，避免丢失修改。',true);return false;
  }
  return true;
}
async function refresh(){
  if(!api)return;
  if(refreshTask)return refreshTask;
  const started=epoch;
  refreshTask=(async()=>{
    try{const next=await api.snapshot();if(started!==epoch)return;state=next;syncedEpoch=started;$('connection-error').hidden=true;render();}
    catch(error){if(started!==epoch)return;syncedEpoch=-1;$('connection-error').hidden=false;render();}
  })();
  try{await refreshTask;}finally{refreshTask=null;}
}
function actBlocksOnUac(action,payload){
  // The bridge waits for the elevated helper, so the form is blocked until UAC is answered.
  return action==='network_save'&&!!state&&payload.startup!==state.network.startup;
}
async function act(action,payload={}){
  if(!api||pending)return false;
  const simulation=savedLocationSource()==='simulation'&&(action==='location_authorize'||(action==='dorm_submit'&&state.dorm.state!=='uncertain'));
  pending=true;epoch++;render();
  if(actBlocksOnUac(action,payload))notify('请在 Windows 提示中选择“是”以授予管理员授权，页面会在授权结束后继续。');
  let ok=false;
  try{const result=await api.dispatch(action,payload);ok=result.ok;notify((simulation?'模拟定位（非实时）· ':'')+result.message,!ok);}
  catch(error){notify('操作未完成，请检查后台连接后重试。',true);}
  finally{pending=false;epoch++;if(refreshTask)await refreshTask;await refresh();render();}
  if(ok&&syncedEpoch!==epoch)notify('操作已执行，但状态尚未同步，请等待状态刷新后再继续。',true);
  return ok&&syncedEpoch===epoch;
}
document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>{
  const action=button.dataset.action;
  if(['network_check','network_start'].includes(action)&&dirty.network){notify('校园网设置尚未保存，请先保存再检测。',true);location.hash='network';return;}
  if(action==='location_authorize'&&!requireSavedLocationSource())return;
  act(action);
}));
$('network-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const payload={username:$('username').value,password:$('password').value,interval:Number($('interval').value),startup:$('startup').checked};
  const revision=revisions.network;
  if(await act('network_save',payload)){if(revision===revisions.network){markDirty('network',false);$('password').value='';}render();}
});
$('location-source').addEventListener('change',()=>{sourceRevision++;sourceDirty=true;markDirty('dorm',true);});
$('dorm-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const payload={enabled:$('auto-enabled').checked,start:$('auto-start').value,end:$('auto-end').value,interval:Number($('auto-interval').value)};
  if(payload.start>=payload.end){notify('结束时间必须晚于开始时间，暂不支持跨日时段。',true);return;}
  const revision=revisions.dorm;
  if(await act('dorm_save',payload)){
    if(revision===revisions.dorm){markDirty('dorm',false);sourceDirty=false;}
    render();
  }
});
$('save-location-source').addEventListener('click',async()=>{
  if(!state||syncedEpoch!==epoch){notify('定位设置尚未同步，请稍后重试。',true);return;}
  const source=$('location-source').value,saved=savedLocationSource();
  if(source===saved){notify('定位来源已是当前设置。');return;}
  const revision=sourceRevision;
  if(await act('location_source_save',{location_source:source})){
    if(revision===sourceRevision){sourceDirty=false;$('location-source').value=savedLocationSource();markDirty('dorm',false);}
    render();
  }
});
/* 「管理静默登录」是折叠区：凭据表单不再和「打卡定位」挤在同一列，也不再让整页多一张
   "表单卡"。展开与否由用户决定，render() 每 1.5 秒重绘也不碰它，所以不会自己弹回去。 */
function setLoginPanel(open){
  $('idm-panel').hidden=!open;
  $('idm-toggle').setAttribute('aria-expanded',open?'true':'false');
  if(open&&!$('idm-username').disabled)$('idm-username').focus();
}
$('idm-toggle').onclick=()=>setLoginPanel($('idm-panel').hidden);
$('idm-credential-form').addEventListener('submit',async event=>{
  event.preventDefault();
  if(!state||syncedEpoch!==epoch){notify('凭据状态尚未同步，请稍后重试。',true);return;}
  const username=$('idm-username').value.trim();
  const password=$('idm-password').value;
  if(!username){notify('请填写统一认证学号。',true);return;}
  if(!password&&!state.dorm.has_idm_credentials){notify('首次保存需要填写密码。',true);return;}
  if(await act('idm_credentials_save',{idm_username:username,idm_password:password})){
    $('idm-password').value='';
    $('idm-save-state').textContent='已保存';
    render();
  }
});
$('idm-clear').onclick=()=>confirmAction('清除已保存的学号密码？',
  '只删除本机保存的统一认证学号密码，下次登录需人工输入。当前学校登录态和校园网账号都不受影响。',
  async()=>{if(await act('idm_credentials_clear')){$('idm-password').value='';$('idm-save-state').textContent='';render();}});
$('toggle-password').addEventListener('click',()=>{const show=$('password').type==='password';$('password').type=show?'text':'password';$('toggle-password').setAttribute('aria-label',show?'隐藏密码':'显示密码');$('toggle-password').setAttribute('aria-pressed',String(show));});
let confirmation=null,previousFocus=null;
function confirmAction(title,text,action){previousFocus=document.activeElement;$('confirm-title').textContent=title;$('confirm-text').textContent=text;confirmation=action;$('confirm-dialog').showModal();$('confirm-cancel').focus();}
$('confirm-cancel').onclick=()=>$('confirm-dialog').close();
$('confirm-dialog').addEventListener('close',()=>{confirmation=null;previousFocus?.focus();});
$('confirm-ok').onclick=()=>{const action=confirmation;$('confirm-dialog').close();if(action)action();};
$('check-updates').onclick=()=>{
  if(!requireUpdateSync())return;
  if(state.update.busy){notify('正在检查、下载或验证更新，请稍候。');return;}
  act('update_check');
};
$('install-update').onclick=()=>{
  if(!requireReadyUpdate())return;
  const version=state.update.latest_version,confirmedEpoch=syncedEpoch;
  confirmAction('安装更新 '+version+'？','确认后将重新验证安装包并打开安装向导，Windows 会请求管理员授权。安装期间会关闭本程序与系统代理，完成后需重新打开程序。打开向导不代表安装成功；取消不会安装，也不会退出程序。',()=>{
    if(!requireReadyUpdate())return;
    if(syncedEpoch!==confirmedEpoch||state.update.latest_version!==version){notify('更新版本或同步状态已变化，请重新确认。',true);return;}
    act('update_install',{confirmed:true,version});
  });
};
$('submit-dorm').onclick=()=>{
  if(!requireSavedLocationSource())return;
  const source=savedLocationSource(),recheck=state.dorm.state==='uncertain';
  const text=recheck
    ? '仅向学校查询已有提交结果，不会重新提交打卡，也不会读取或发送新的位置坐标。'
    : (source==='simulation'
      ? '将使用已保存的模拟位置（样本附近的随机偏移，非实时，并非当前位置），向学校发送登录凭据、任务字段和该位置。'
      : '将使用 Windows 实时定位，向学校发送登录凭据、任务字段和位置。')+'仍须通过学校验证，学校仍可拒绝。提交后会回查学校结果。';
  confirmAction(recheck?'回查提交结果？':'提交今日打卡？',text,()=>{
    if(!requireSavedLocationSource())return;
    if(source!==savedLocationSource()||recheck!==(state.dorm.state==='uncertain')){notify('定位来源或任务状态已变化，请重新确认。',true);return;}
    act(recheck?'dorm_query':'dorm_submit');
  });
};
$('logout-dorm').onclick=()=>confirmAction('退出学校登录？','这会同时关闭自动打卡。校园网账号不受影响，下次打卡需要重新登录学校账号。',async()=>{if(await act('dorm_logout')){markDirty('dorm',false);render();}});

/* 打卡账号 ---------------------------------------------------------------------------------
   所有开启了自动打卡的账号都会在各自时段内自动打卡，但 state.dorm 仍然只描述活动账号，切换
   后整页设置跟着换，所以前端不为每个账号留一份表单副本。账号栏因此要一眼看出每个账号今天的
   状态：下拉框后缀和汇总文字都直接读快照。老快照可能完全没有 accounts 层，那时整条账号栏不
   出现；也可能缺 busy/enabled/signed_today，一律按假值处理，其余功能照常。 */
function accountItems(){return state?.accounts?.items||[];}
function activeAccount(){
  const items=accountItems();
  return items.find(account=>account.active)||items.find(account=>account.id===state?.accounts?.active)||null;
}
function activeAccountId(){const active=activeAccount();return active?active.id:'';}
// 后缀按优先级只取第一个命中的。顺序是有意的：没开自动打卡的账号，它上一次的状态只是历史，
// 不该盖过「未开启自动打卡」这句更该让人知道的话。
const accountSuffixState={login_required:' · 需要重新登录',location_required:' · 需要检查定位',error:' · 需要检查',network_error:' · 需要检查',ready:' · 今日待打卡',no_task:' · 今天暂无任务'};
function accountLabel(account){
  const suffix=account.missing?' · 档案缺失'
    :account.busy?' · 处理中'
    :account.signed_today?' · 今日已完成'
    :!account.enabled?' · 未开启自动打卡'
    :accountSuffixState[account.state]||' · 等待时段';
  return account.name+(account.idm_username?` · ${account.idm_username}`:'')+suffix;
}
// 汇总只在多账号时出现：单个账号的自动打卡状态在下面的设置卡片里已经说清楚了。
function accountSummary(items){
  if(items.length<2)return '';
  const enabledCount=items.filter(account=>account.enabled).length;
  if(!enabledCount)return '';
  return `自动打卡：${enabledCount} 个账号已开启 · ${items.filter(account=>account.signed_today).length} 个今日已完成`;
}
function requireAccountSync(){
  if(!state||syncedEpoch!==epoch){notify('账号状态尚未同步，请稍后重试。',true);return false;}
  // 清单读不出来时后端会拒绝每一个账号操作，这里先按它的原话说明，不让按钮白点。
  if(state.accounts?.error){notify(state.accounts.error,true);return false;}
  return true;
}
/* 学校登录 ---------------------------------------------------------------------------------
   登录态只有后端能回答：快照里的 has_session 直接读本机保存的学校会话。不用 state 反推 ——
   「今日已完成」「等待时段」「自动打卡已关闭」都在读会话**之前**就返回了，反推会把「今天没查过」
   说成「已登录」。宁可让人多点一次「学校登录」，也不能让人以为已经登录。
   老快照没有这个字段，那时一律说「尚未确认」，不猜。
   凭据与账号归属：统一认证凭据存在每个打卡账号自己的档案里，所以这里必须点名是谁的，
   否则切换账号后这张卡的归属会静默改变，而用户看不出来。 */
function renderLogin(d,pending){
  const session=d.has_session,account=activeAccount();
  const expired=d.state==='login_required',loggedIn=session===true;
  const subject=account?`打卡账号「${account.name}」`:'本机';
  badge('login-badge',
    expired?'登录已失效':loggedIn?'已登录':session===false?'未登录':'尚未确认',
    expired?'error':loggedIn?'success':session===false?'warning':'');
  $('login-line').textContent=expired
    ?`${subject}的学校登录已失效，点「学校登录 / 重新登录」才能继续打卡。`
    :loggedIn?`${subject}已登录学校账号。`
    :session===false?`${subject}尚未登录学校账号，点「学校登录 / 重新登录」即可开始。`
    :`${subject}的登录状态尚未确认，可以先查询今日任务。`;
  $('idm-credential-state').textContent=d.has_idm_credentials
    ?`静默登录已就绪（学号 ${d.idm_username||'未知'}）`
    :'静默登录未设置：每次登录需手动输入学号密码和验证码';
  $('idm-toggle').textContent=d.has_idm_credentials?'管理静默登录':'设置静默登录';
  $('idm-toggle').disabled=pending||d.busy;
}
function renderAccounts(){
  const bar=$('account-bar');
  if(!bar)return;
  const accounts=state.accounts||{},items=accounts.items||[],error=accounts.error||'';
  bar.hidden=!(items.length||error);
  const select=$('dorm-account');
  select.replaceChildren(...items.map(account=>{
    const option=document.createElement('option');
    option.value=account.id;
    option.textContent=accountLabel(account);
    return option;
  }));
  const active=activeAccount(),activeId=active?active.id:'';
  if(syncedEpoch===epoch)select.value=activeId;   // 状态没同步时不覆盖用户正在做的选择
  // 切换账号只是界面行为：打卡中的账号可以照常选、照常删，后端会拒绝动作并给出原因。
  const blocked=Boolean(pending||error),max=accounts.max||0,full=max>0&&items.length>=max;
  select.disabled=blocked||!items.length;
  $('account-name').disabled=blocked;
  $('account-add').disabled=blocked||full;
  $('account-rename').disabled=blocked||!items.length;
  $('account-delete').disabled=blocked||!items.length;
  $('account-state').textContent=error||(full?`最多 ${max} 个账号`:'')||accountSummary(items);
  // 名字框只在真的换了活动账号时回写：它同时也是新建账号的输入框，不能被后台重绘冲掉。
  if(accountNameFor!==activeId){
    accountNameFor=activeId;
    $('account-name').value=active?active.name:'';
  }
}
$('dorm-account').addEventListener('change',()=>{
  const id=$('dorm-account').value;
  if(!id||id===activeAccountId())return;             // 选中的就是活动账号：什么都不做
  const confirmedEpoch=syncedEpoch;
  const switching=async()=>{
    if(!requireAccountSync())return;
    if(syncedEpoch!==confirmedEpoch){notify('账号状态已变化，请重新选择要切换的账号。',true);render();return;}
    if(await act('account_switch',{id})){
      closeMap();
      $('account-name').value='';
      accountNameFor=null;
      markDirty('dorm',false);
      sourceDirty=false;
    }
    render();
  };
  if(dirty.dorm||sourceDirty){
    confirmAction('切换账号？','寝室打卡设置或定位来源还有未保存的修改，切换账号会放弃这些修改。',switching);
    $('dorm-account').value=activeAccountId();       // 还没切换成功，下拉框先回到活动账号
  }else switching();
});
$('account-add').onclick=async()=>{
  if(!requireAccountSync())return;
  if(await act('account_add',{name:$('account-name').value})){
    $('account-name').value='';
    markDirty('dorm',false);
    sourceDirty=false;
  }
  render();
};
$('account-rename').onclick=async()=>{
  const active=activeAccount();
  if(!active||!requireAccountSync())return;
  const name=$('account-name').value;
  if(!name.trim()){notify('请填写账号名称，最多 24 个字。',true);return;}
  if(await act('account_rename',{id:active.id,name}))accountNameFor=null;   // 按后端清洗后的名字回填
  render();
};
$('account-delete').onclick=()=>{
  const active=activeAccount();
  if(!active||!requireAccountSync())return;
  confirmAction(`删除账号「${active.name}」？`,'会同时删除这个账号在本机的打卡设置、已保存的打卡点、当日打卡记录和登录凭据；无法撤销。',async()=>{
    await act('account_delete',{id:active.id,confirmed:true});
    render();
  });
};
$('quit').onclick=()=>confirmAction('退出 youziauth？','退出将停止本程序的后台检测和自动打卡。已启用的系统认证代理仍会运行。',()=>act('quit'));
$('refresh-records').onclick=async()=>{await refresh();if($('connection-error').hidden)notify('运行记录已刷新');};
document.querySelectorAll('[data-log]').forEach(button=>button.onclick=()=>{logType=button.dataset.log;document.querySelectorAll('[data-log]').forEach(b=>{b.classList.toggle('selected',b===button);b.setAttribute('aria-pressed',String(b===button));});renderLogs();});

/* Simulation map picker ---------------------------------------------------------------------
   The one place this UI renders coordinates: the point the user chooses to submit, and the
   school's own published check-in point. A live Windows fix is never drawn here, and the whole
   card stays closed until the saved source is 模拟定位 and the user opens it. */
function mapScale(zoom){return MAP_TILE*Math.pow(2,zoom);}
function mapX(lng,zoom){return (lng+180)/360*mapScale(zoom);}
function mapY(lat,zoom){const s=Math.sin(lat*Math.PI/180);return (0.5-Math.log((1+s)/(1-s))/(4*Math.PI))*mapScale(zoom);}
function mapLng(x,zoom){return x/mapScale(zoom)*360-180;}
function mapLat(y,zoom){const n=Math.PI-2*Math.PI*y/mapScale(zoom);return 180/Math.PI*Math.atan(0.5*(Math.exp(n)-Math.exp(-n)));}
function mapMetresPerPixel(lat,zoom){return 156543.03392*Math.cos(lat*Math.PI/180)/Math.pow(2,zoom);}
function mapToGcj02(latitude,longitude){
  // Byte-for-byte the same offset dorm_location.wgs84_to_gcj02 applies, so the browser and the
  // bridge cannot disagree about where a GCJ02 basemap draws a place.
  if(!(longitude>=72.004&&longitude<=137.8347&&latitude>=0.8293&&latitude<=55.8271))
    return {latitude,longitude};
  const x=longitude-105,y=latitude-35,ee=.00669342162296594323;
  let a=-100+2*x+3*y+.2*y*y+.1*x*y+.2*Math.sqrt(Math.abs(x));
  let b=300+x+2*y+.1*x*x+.1*x*y+.1*Math.sqrt(Math.abs(x));
  const wave=(20*Math.sin(6*x*Math.PI)+20*Math.sin(2*x*Math.PI))*2/3;
  a+=wave+(20*Math.sin(y*Math.PI)+40*Math.sin(y*Math.PI/3))*2/3;
  a+=(160*Math.sin(y*Math.PI/12)+320*Math.sin(y*Math.PI/30))*2/3;
  b+=wave+(20*Math.sin(x*Math.PI)+40*Math.sin(x*Math.PI/3))*2/3;
  b+=(150*Math.sin(x*Math.PI/12)+300*Math.sin(x*Math.PI/30))*2/3;
  const rad=latitude*Math.PI/180,magic=1-ee*Math.pow(Math.sin(rad),2),root=Math.sqrt(magic);
  a=a*180/((6378245*(1-ee))/(magic*root)*Math.PI);
  b=b*180/(6378245/root*Math.cos(rad)*Math.PI);
  return {latitude:latitude+a,longitude:longitude+b};
}
/* Basemap providers -------------------------------------------------------------------------
   Tiles come from a third party, and that third party can fail. OpenStreetMap's volunteer servers
   block app-like traffic outright (HTTP 200 plus an "Access blocked" PNG and an `x-blocked`
   header) and are slow, or entirely unreachable, from mainland China. Worse, a request can simply
   hang: no load, no error, which is the bare-grid report this change fixes. So tiles are retried,
   a provider that draws nothing within MAP_TILE_STALL hands the frame to the next one, and the
   status line says which provider is drawing.
   There is deliberately no pre-flight probe. One was written first and had to be dropped: on a
   measured campus connection the very first HEAD request to OpenStreetMap from a cold WebView2
   timed out at 30 s while the tiles themselves arrived, so probing only replaced a working basemap
   with the fallback. The block case is not detectable from an <img> at all - the block body is a
   perfectly valid PNG - and it is left to the status line to say the basemap is not coming. */
function mapProviders(state){
  const listed=(state&&state.providers||[]).filter(provider=>provider&&provider.url);
  if(listed.length)return listed;
  // Bridges from before the fallback shipped send one provider as three loose fields.
  return state&&state.tile_url
    ?[{id:'default',name:'',url:state.tile_url,attribution:state.attribution||'',
       max_zoom:state.max_zoom||MAP_MAX_ZOOM,crs:'wgs84'}]
    :[];
}
function mapProvider(state){
  const list=mapProviders(state);
  return list[(state&&state.provider_index)||0]||list[0]||null;
}
function mapTileUrl(provider,zoom,x,y){
  return provider.url.replace('{z}',zoom).replace('{x}',x).replace('{y}',y);
}
function mapMaxZoom(state){
  const provider=mapProvider(state);
  const limit=provider&&provider.max_zoom?provider.max_zoom:MAP_MAX_ZOOM;
  return Math.max(MAP_MIN_ZOOM,Math.min(MAP_MAX_ZOOM,limit));
}
function mapTileCentre(state,provider){
  // A GCJ02 provider draws its own offset grid. Projecting tiles from the offset centre while
  // markers and picks stay on the plain WGS84 centre makes the offset cancel, so campus features
  // and pins line up while every coordinate the picker produces is still WGS84. It is exact only
  // while the offset is locally constant; measured over the whole zoom range on the campus this is
  // used at (.scratch/wb2probe/gcj_alignment.py) the residual stays under ~1.1 px, and 0.5 px at
  // the zoom the picker opens on - far below the 7 px marker dot and the 100 m+ check-in radius.
  const chosen=provider||mapProvider(state);
  if(!chosen||chosen.crs!=='gcj02')return state.center;
  return mapToGcj02(state.center.latitude,state.center.longitude);
}
function mapTileCounts(state){
  // Only the level on screen counts. drawMapTiles skips every other level, so a stale-zoom tile
  // must not decide the credit, the failover or the status line either.
  const tiles=Object.entries(state.tiles||{})
    .filter(([key])=>Number(key.split('/')[0])===state.zoom)
    .map(entry=>entry[1]);
  return {total:tiles.length,ready:tiles.filter(tile=>tile.ready).length,
          failed:tiles.filter(tile=>tile.failed).length,
          pending:tiles.filter(tile=>!tile.ready&&!tile.failed).length};
}
function mapBasemapFailed(state){
  if(state.basemap_stalled)return true;
  const counts=mapTileCounts(state);
  return counts.failed>0&&counts.ready===0&&counts.pending===0;
}
function mapStopStall(state){
  if(state.basemap_stall)clearTimeout(state.basemap_stall);
  state.basemap_stall=null;
}
function mapDropTiles(state){
  mapStopStall(state);
  for(const tile of Object.values(state.tiles||{}))if(tile.timer)clearTimeout(tile.timer);
  state.tiles={};
  state.basemap_stalled=false;
}
function mapFailOver(state){
  // Hand the frame to the next provider. Bounded: it only ever walks forward.
  const list=mapProviders(state);
  const next=(state.provider_index||0)+1;
  if(next>=list.length)return false;
  state.provider_index=next;
  mapDropTiles(state);
  if(mapState===state)renderMap();
  return true;
}
function mapWatchStall(state){
  // The fast path for a frame where nothing at all has drawn: fail over at once instead of letting
  // every tile burn its own deadline and retries first. A partly-drawn frame keeps its tiles and
  // relies on their individual deadlines, so this guard is not the only way out of a hang.
  if(state.basemap_stall||state.basemap_stalled)return;
  state.basemap_stall=setTimeout(()=>{
    state.basemap_stall=null;
    if(mapState!==state)return;
    const counts=mapTileCounts(state);
    if(!counts.total||counts.ready)return;      // map data arrived after all
    if(mapFailOver(state))return;
    state.basemap_stalled=true;                 // nothing left to try: report it instead of waiting
    renderMap();
  },MAP_TILE_STALL);
}
function mapDistance(from,to){
  const radius=6371000,first=from.latitude*Math.PI/180,second=to.latitude*Math.PI/180;
  const dLat=second-first,dLng=(to.longitude-from.longitude)*Math.PI/180;
  const a=Math.sin(dLat/2)**2+Math.cos(first)*Math.cos(second)*Math.sin(dLng/2)**2;
  return 2*radius*Math.asin(Math.min(1,Math.sqrt(a)));
}
function mapSize(canvas){return {width:canvas.width||640,height:canvas.height||360};}
function mapEventPoint(event){
  // Canvas coordinates, not CSS pixels: the canvas is stretched to the dialog width. offsetX is
  // measured from the padding edge while getBoundingClientRect returns the border box, so the 1 px
  // frame has to come off before scaling - dividing by the border box skews every pick outwards as
  // it approaches the right and bottom edges, by about a metre at zoom 17.
  const canvas=$('map-canvas');
  let x=event.offsetX,y=event.offsetY;
  if(typeof x!=='number'||typeof y!=='number')return null;
  const rect=canvas.getBoundingClientRect?canvas.getBoundingClientRect():null;
  if(rect&&rect.width&&canvas.width){
    const style=typeof getComputedStyle==='function'?getComputedStyle(canvas):null;
    const borderX=style?(parseFloat(style.borderLeftWidth)||0)+(parseFloat(style.borderRightWidth)||0):0;
    const borderY=style?(parseFloat(style.borderTopWidth)||0)+(parseFloat(style.borderBottomWidth)||0):0;
    const innerWidth=rect.width-borderX,innerHeight=rect.height-borderY;
    x=x*canvas.width/(innerWidth>0?innerWidth:rect.width);
    y=y*canvas.height/(innerHeight>0?innerHeight:rect.height);
  }
  return {x,y};
}
function zoomMap(step){
  if(!mapState)return;
  mapState.zoom=Math.max(MAP_MIN_ZOOM,Math.min(mapMaxZoom(mapState),mapState.zoom+step));
  mapDropTiles(mapState);                  // a new zoom needs new tiles
  renderMap();
}
function panMap(dx,dy){
  // Dragging right moves the map right, so the centre travels the other way.
  const zoom=mapState.zoom;
  mapState.center={latitude:mapLat(mapY(mapState.center.latitude,zoom)-dy,zoom),
                   longitude:mapLng(mapX(mapState.center.longitude,zoom)-dx,zoom)};
  renderMap();
}
function mapScreen(state,lat,lng,size){
  return {x:mapX(lng,state.zoom)-mapX(state.center.longitude,state.zoom)+size.width/2,
          y:mapY(lat,state.zoom)-mapY(state.center.latitude,state.zoom)+size.height/2};
}
function mapPointAt(state,size,x,y){
  return {latitude:mapLat(mapY(state.center.latitude,state.zoom)+(y-size.height/2),state.zoom),
          longitude:mapLng(mapX(state.center.longitude,state.zoom)+(x-size.width/2),state.zoom)};
}
function mapBounds(state,size){
  const topLeft=mapPointAt(state,size,0,0),bottomRight=mapPointAt(state,size,size.width,size.height);
  return {north:topLeft.latitude,west:topLeft.longitude,south:bottomRight.latitude,east:bottomRight.longitude};
}
function mapRange(state,point){
  if(!point||!state.reference)return null;
  const metres=mapDistance(point,state.reference),limit=state.reference.radius_m;
  return {metres,inRange:limit?metres<=limit:null};
}
function mapRound(value){return Math.round(value*1e6)/1e6;}
function mapBadge(state){
  const target=state.picked||state.point;
  if(!target)return '未选择位置';
  const prefix=state.picked?'待保存 · ':'已保存 · ';
  const range=mapRange(state,target);
  if(!range)return prefix+'距基准点未知';
  const within=range.inRange===false?'（超出范围）':range.inRange===true?'（范围内）':'';
  return prefix+'距基准点 '+Math.round(range.metres)+' 米'+within;
}
function drawMapGrid(ctx,state,size){
  const bounds=mapBounds(state,size),step=0.002;   // ~220 m of latitude
  ctx.save();ctx.strokeStyle='rgba(93,113,133,.22)';ctx.lineWidth=1;
  for(let lat=Math.ceil(bounds.south/step)*step;lat<=bounds.north;lat+=step){
    const point=mapScreen(state,lat,bounds.west,size);
    ctx.beginPath();ctx.moveTo(0,point.y);ctx.lineTo(size.width,point.y);ctx.stroke();
  }
  for(let lng=Math.ceil(bounds.west/step)*step;lng<=bounds.east;lng+=step){
    const point=mapScreen(state,bounds.north,lng,size);
    ctx.beginPath();ctx.moveTo(point.x,0);ctx.lineTo(point.x,size.height);ctx.stroke();
  }
  ctx.restore();
  const width=100/mapMetresPerPixel(state.center.latitude,state.zoom);
  if(width>=24&&width<=size.width/3){
    ctx.save();ctx.strokeStyle='#4a5b6b';ctx.lineWidth=2;
    ctx.beginPath();ctx.moveTo(12,size.height-14);ctx.lineTo(12+width,size.height-14);ctx.stroke();
    ctx.fillStyle='#4a5b6b';ctx.font='11px system-ui';ctx.fillText('100 米',12,size.height-20);ctx.restore();
  }
}
function drawMapTiles(ctx,state,size){
  if(!state.useTiles||!state.tiles)return;
  const centre=mapTileCentre(state);
  const centreX=mapX(centre.longitude,state.zoom),centreY=mapY(centre.latitude,state.zoom);
  for(const [key,tile] of Object.entries(state.tiles)){
    const [zoom,x,y]=key.split('/').map(Number);
    if(zoom!==state.zoom||!tile.ready)continue;
    ctx.drawImage(tile.image,x*MAP_TILE-(centreX-size.width/2),y*MAP_TILE-(centreY-size.height/2),MAP_TILE,MAP_TILE);
  }
}
function mapTileOwner(key,tile){
  // A tile is live iff the picker still holds this exact object under this key. Comparing the state
  // object instead was a bug: a reload (saving, switching, renaming or deleting a point with the
  // dialog open) replaces mapState but keeps the tile map, so every in-flight tile lost its
  // repaint, its retry and its share of the failover.
  return mapState&&mapState.tiles&&mapState.tiles[key]===tile?mapState:null;
}
function mapTileDeadline(key,tile){
  // A request can hang: no load, no error. That is the failure this whole change exists for, so
  // every tile carries its own deadline - one tile arriving must not excuse the others.
  clearTimeout(tile.timer);
  tile.timer=setTimeout(()=>{
    tile.timer=null;
    if(!mapTileOwner(key,tile)||tile.ready||tile.failed)return;
    mapTileFailed(key,tile);
  },MAP_TILE_STALL);
}
function mapStartTile(state,key,tile){
  const provider=mapProvider(state);
  if(!provider)return;
  const [zoom,x,y]=key.split('/').map(Number);
  tile.attempts+=1;
  tile.ready=false;
  tile.failed=false;
  const image=new Image();
  tile.image=image;
  image.onload=()=>{
    const live=mapTileOwner(key,tile);     // a provider switch or a close retires the tile
    if(!live)return;
    tile.ready=true;
    live.basemap_stalled=false;            // a late tile contradicts "nothing is coming"
    renderMap();
  };
  image.onerror=()=>{
    if(!mapTileOwner(key,tile))return;
    mapTileFailed(key,tile);
  };
  image.src=mapTileUrl(provider,zoom,x,y);
  mapTileDeadline(key,tile);
}
function mapTileFailed(key,tile){
  const state=mapTileOwner(key,tile);
  if(!state)return;
  tile.ready=false;
  clearTimeout(tile.timer);
  if(tile.attempts<MAP_TILE_TRIES){
    // A blip must not poison the tile for the rest of the session: this used to be a one-way
    // "failed" flag, so one dropped request left a bare grid until the dialog was reopened.
    tile.timer=setTimeout(()=>{
      tile.timer=null;
      const live=mapTileOwner(key,tile);
      if(!live)return;
      mapStartTile(live,key,tile);
    },MAP_TILE_BACKOFF[Math.min(tile.attempts-1,MAP_TILE_BACKOFF.length-1)]);
    return;
  }
  tile.failed=true;
  if(mapCheckBasemap(state))renderMap();   // nothing left to try: show it on the status line
}
function mapCheckBasemap(state){
  const counts=mapTileCounts(state);
  if(!counts.total)return false;
  if(counts.ready||counts.pending)return false;
  if(mapFailOver(state))return false;
  return true;
}
function loadMapTiles(state,size){
  if(!state.useTiles||typeof Image!=='function')return;
  if(!mapProvider(state))return;
  const zoom=Math.max(MAP_MIN_ZOOM,Math.min(mapMaxZoom(state),state.zoom));
  state.tiles=state.tiles||{};
  const centre=mapTileCentre(state);
  const centreX=mapX(centre.longitude,zoom),centreY=mapY(centre.latitude,zoom);
  const span=Math.pow(2,zoom);
  const first={x:Math.floor((centreX-size.width/2)/MAP_TILE),y:Math.floor((centreY-size.height/2)/MAP_TILE)};
  const last={x:Math.floor((centreX+size.width/2)/MAP_TILE),y:Math.floor((centreY+size.height/2)/MAP_TILE)};
  for(let x=first.x;x<=last.x;x++)for(let y=first.y;y<=last.y;y++){
    if(y<0||y>=span)continue;
    const column=((x%span)+span)%span,key=zoom+'/'+column+'/'+y;
    if(state.tiles[key])continue;
    const tile={image:null,ready:false,failed:false,attempts:0,timer:null};
    state.tiles[key]=tile;
    mapStartTile(state,key,tile);
  }
  if(!mapTileCounts(state).ready)mapWatchStall(state);
}
function drawMapMarker(ctx,point,colour,label){
  ctx.save();ctx.beginPath();ctx.arc(point.x,point.y,7,0,Math.PI*2);
  ctx.fillStyle=colour;ctx.fill();ctx.strokeStyle='#fff';ctx.lineWidth=2;ctx.stroke();
  ctx.fillStyle=colour;ctx.font='12px system-ui';ctx.fillText(label,point.x+11,point.y+4);ctx.restore();
}
function mapPendingLabel(state){
  // While the user is typing a name, the pending marker carries it: the label they see on the map
  // is the one that will be saved.
  const typed=($('map-point-name').value||'').trim();
  return typed?`待保存：${typed}`:'待保存';
}
function mapSavedLabel(state){
  const active=(state.points||[]).find(point=>point.active);
  if(active)return active.name;
  return state.point&&state.point.picked?'已保存（地图选点）':'已保存（本机样本）';
}
function mapCredit(state){
  // The map data needs its credit; the licence asks for it, so it is drawn on the canvas instead
  // of taking a paragraph of its own. Only while third-party data is actually on the canvas: with
  // the basemap off, or with every tile still failing, the grid underneath is ours alone.
  if(!state.useTiles)return '';
  if(!mapTileCounts(state).ready)return '';
  const provider=mapProvider(state);
  return (provider&&provider.attribution)||state.attribution||'© OpenStreetMap contributors';
}
function mapBasemapNote(state){
  // The reported bug was silence: a failed basemap and a switched-off one looked identical.
  if(!state.useTiles)return '';
  const counts=mapTileCounts(state);
  const onFallback=(state.provider_index||0)>0;
  const provider=mapProvider(state);
  if(mapBasemapFailed(state))
    return '在线底图暂时连不上，已回退离线网格。点「重试底图」可再试一次。';
  if(counts.ready&&onFallback&&provider&&provider.name)
    return '首选底图暂时连不上，已改用备用底图：'+provider.name+'。';
  if(!counts.ready&&counts.pending)return '正在加载在线底图…';
  return '';
}
function renderBasemapState(state){
  const note=$('map-basemap-note');
  if(note){
    const text=mapBasemapNote(state);
    note.hidden=!text;
    note.textContent=text;
    note.classList.toggle('warn',mapBasemapFailed(state));
  }
  const retry=$('map-basemap-retry');
  if(retry)retry.hidden=!mapBasemapFailed(state);
}
function drawMap(ctx,canvas,state){
  const size=mapSize(canvas);
  ctx.clearRect(0,0,size.width,size.height);
  ctx.fillStyle='#eef1f4';ctx.fillRect(0,0,size.width,size.height);
  drawMapGrid(ctx,state,size);
  loadMapTiles(state,size);
  drawMapTiles(ctx,state,size);
  if(state.reference){
    const point=mapScreen(state,state.reference.latitude,state.reference.longitude,size);
    if(state.reference.radius_m){
      const radius=state.reference.radius_m/mapMetresPerPixel(state.reference.latitude,state.zoom);
      ctx.save();ctx.beginPath();ctx.arc(point.x,point.y,radius,0,Math.PI*2);
      ctx.fillStyle='rgba(52,120,196,.10)';ctx.fill();
      ctx.strokeStyle='rgba(52,120,196,.55)';ctx.setLineDash([6,4]);ctx.stroke();ctx.restore();
    }
    drawMapMarker(ctx,point,'#2f6fb2','学校打卡点');
  }
  if(state.point){
    drawMapMarker(ctx,mapScreen(state,state.point.latitude,state.point.longitude,size),'#c2703a',
                  mapSavedLabel(state));
  }
  if(state.picked)drawMapMarker(ctx,mapScreen(state,state.picked.latitude,state.picked.longitude,size),'#c0392b',
                                mapPendingLabel(state));
  const credit=mapCredit(state);
  if(credit){
    ctx.save();ctx.font='10px system-ui';ctx.textAlign='right';
    ctx.fillStyle='rgba(41,51,62,.6)';
    ctx.fillText(credit,size.width-8,size.height-6);
    ctx.restore();
  }
}
function mapPointLabel(point){
  const range=mapRange(mapState,point);
  return range?`${point.name} · ${Math.round(range.metres)} 米`:point.name;
}
function renderPoints(){
  const select=$('map-points');
  if(!select||!mapState)return;
  const points=mapState.points||[];
  const options=points.map(point=>{
    const option=document.createElement('option');
    option.value=point.id;
    option.textContent=mapPointLabel(point);
    return option;
  });
  if(mapState.point&&!points.some(point=>point.active)){
    // The replayed sample is not one of the saved points: a captured sample, or a deleted point.
    const option=document.createElement('option');
    option.value='';
    option.textContent='当前样本（未命名）';
    options.unshift(option);
  }
  select.replaceChildren(...options);
  select.value=mapState.active_id||'';
  select.disabled=!options.length;
  const active=points.find(point=>point.active);
  $('map-point-state').textContent=active
    ? `当前生效：${active.name}${active.saved_at?'（'+active.saved_at.slice(0,16).replace('T',' ')+'）':''}`
    : (mapState.point?'当前生效：未命名的样本。':'尚未保存任何选点：在地图上点击后保存。');
  // The name box follows the active point, but only when it actually changes: it used to be
  // rewritten on every redraw, and tile loads redraw constantly, so typing was being wiped.
  const activeId=active?active.id:'';
  if(mapNameFor!==activeId){
    mapNameFor=activeId;
    $('map-point-name').value=active?active.name:'';
  }
  $('map-point-rename').disabled=!active;
  $('map-point-delete').disabled=!active;
}
function renderMap(){
  const canvas=$('map-canvas');
  if(!canvas||!mapState)return;
  mapState.useTiles=$('map-tiles').checked;
  // 高德 has no zoom 19: past its top level the tiles come back blank, so the frame follows the
  // provider's own ceiling instead of the global one.
  mapState.zoom=Math.max(MAP_MIN_ZOOM,Math.min(mapMaxZoom(mapState),mapState.zoom));
  $('map-distance-badge').textContent=mapBadge(mapState);
  $('map-summary').textContent=mapState.reference
    ? '学校打卡点：'+(mapState.reference.address||'以学校任务为准')+'，允许半径约 '+
      (mapState.reference.radius_m?Math.round(mapState.reference.radius_m)+' 米':'未知')+'。点击地图放置标记。'
    : '尚未查询今日任务，地图上没有学校基准点；仍可先选点并保存，查询任务后即可核对距离。';
  $('map-save').disabled=!mapState.picked;
  renderPoints();
  const ctx=canvas.getContext?canvas.getContext('2d'):null;
  if(ctx)drawMap(ctx,canvas,mapState);
  // After the draw, never before: the status line reports the tile state the draw just settled.
  renderBasemapState(mapState);
}
function applyMapModel(model){
  const centre=model.reference||model.point||mapState?.center||{latitude:0,longitude:0};
  // A reload (saving or switching a point) keeps the provider the picker already settled on.
  mapState={...model,center:{latitude:centre.latitude,longitude:centre.longitude},
            zoom:mapState?.zoom||17,picked:null,tiles:mapState?.tiles||{},
            provider_index:mapState?.provider_index||0,
            basemap_stalled:false,basemap_stall:null};
  const dialog=$('map-dialog');
  if(dialog&&!dialog.open)dialog.showModal();
  renderMap();render();
}
function clearMapState(){
  if(mapState)mapDropTiles(mapState);
  mapState=null;
  $('map-save').disabled=true;
  $('map-distance-badge').textContent='未选择位置';
  const note=$('map-basemap-note');
  if(note){note.hidden=true;note.textContent='';note.classList.toggle('warn',false);}
  const retry=$('map-basemap-retry');
  if(retry)retry.hidden=true;
}
function closeMap(){
  clearMapState();
  const dialog=$('map-dialog');
  if(dialog&&dialog.open)dialog.close();
}
async function loadMapModel(){
  if(!api||!api.simulation_map)throw new Error('地图选点需要桌面程序支持，请更新后再试。');
  const model=await api.simulation_map();
  if(!model||model.ok===false)throw new Error(model?.message||'地图数据读取失败，请稍后重试。');
  return model;
}
async function openMap(){
  if(!state||syncedEpoch!==epoch){notify('定位设置尚未同步，请稍后重试。',true);return;}
  if(savedLocationSource()!=='simulation'){notify('请先把定位来源保存为模拟定位，再使用地图选点。',true);return;}
  try{applyMapModel(await loadMapModel());}
  catch(error){notify(error.message,true);}
}
async function reloadMap(){
  try{applyMapModel(await loadMapModel());}catch{closeMap();render();}
}
$('open-map-picker').onclick=openMap;
$('map-close').onclick=()=>{closeMap();render();};
$('map-dialog').addEventListener('close',()=>{clearMapState();render();});
$('map-zoom-in').onclick=()=>zoomMap(1);
$('map-zoom-out').onclick=()=>zoomMap(-1);
$('map-recenter').onclick=()=>{
  if(!mapState)return;
  const target=mapState.reference||mapState.point;
  if(target)mapState.center={latitude:target.latitude,longitude:target.longitude};
  renderMap();
};
$('map-tiles').addEventListener('change',()=>{
  if(!mapState)return;
  // Switching the basemap back on must clear a previous failure, not just the tiles.
  mapDropTiles(mapState);
  renderMap();
});
$('map-basemap-retry').onclick=()=>{
  // Back to the preferred provider with a clean slate: the manual version of the automatic retry,
  // for when the network came back after the dialog was already open.
  if(!mapState)return;
  mapState.provider_index=0;
  mapDropTiles(mapState);
  renderMap();
};
$('map-canvas').addEventListener('wheel',event=>{
  if(!mapState)return;
  event.preventDefault();
  zoomMap(event.deltaY<0?1:-1);
});
$('map-canvas').addEventListener('pointerdown',event=>{
  if(!mapState)return;
  const point=mapEventPoint(event);
  if(!point)return;
  mapDrag={x:point.x,y:point.y};
  mapSuppressPick=false;
  const canvas=$('map-canvas');
  canvas.setPointerCapture?.(event.pointerId);
  canvas.classList.toggle('dragging',true);
});
$('map-canvas').addEventListener('pointermove',event=>{
  if(!mapState||!mapDrag)return;
  const point=mapEventPoint(event);
  if(!point)return;
  const dx=point.x-mapDrag.x,dy=point.y-mapDrag.y;
  if(Math.abs(dx)<2&&Math.abs(dy)<2)return;   // a twitchy hand is still a click
  mapSuppressPick=true;
  mapDrag={x:point.x,y:point.y};
  panMap(dx,dy);
});
for(const ending of ['pointerup','pointercancel']){
  $('map-canvas').addEventListener(ending,()=>{
    mapDrag=null;
    $('map-canvas').classList.toggle('dragging',false);
  });
}
$('map-points').addEventListener('change',async()=>{
  const id=$('map-points').value;
  if(!mapState||!id||id===mapState.active_id)return;
  if(await act('simulation_point_select',{id}))await reloadMap();
});
$('map-point-name').addEventListener('input',()=>{if(mapState)renderMap();});
$('map-point-rename').onclick=async()=>{
  const active=(mapState?.points||[]).find(entry=>entry.active);
  if(!active)return;
  const name=$('map-point-name').value;
  if(await act('simulation_point_rename',{id:active.id,name})){
    mapNameFor=null;                    // refresh the box from the stored (cleaned) name
    await reloadMap();
  }
};
$('map-point-delete').onclick=()=>{
  const active=(mapState?.points||[]).find(entry=>entry.active);
  if(!active)return;
  confirmAction(`删除选点「${active.name}」？`,'删除只影响这一个选点；如果它是当前生效的点，会自动切换到列表里的下一个。',async()=>{
    if(await act('simulation_point_delete',{id:active.id}))await reloadMap();
  });
};
$('map-canvas').addEventListener('click',event=>{
  if(!mapState)return;
  if(mapSuppressPick){mapSuppressPick=false;return;}   // the click that ended a drag is not a pick
  const point=mapEventPoint(event);
  if(!point)return;
  const target=mapPointAt(mapState,mapSize($('map-canvas')),point.x,point.y);
  mapState.picked={latitude:mapRound(target.latitude),longitude:mapRound(target.longitude)};
  renderMap();
});
$('map-canvas').addEventListener('keydown',event=>{
  if(!mapState)return;
  if(event.key==='+'||event.key==='='){zoomMap(1);event.preventDefault();return;}
  if(event.key==='-'||event.key==='_'){zoomMap(-1);event.preventDefault();return;}
  const metres=event.shiftKey?200:50;
  const moves={ArrowLeft:[-metres,0],ArrowRight:[metres,0],ArrowUp:[0,metres],ArrowDown:[0,-metres]};
  const move=moves[event.key];
  if(!move)return;
  event.preventDefault();
  mapState.center={latitude:mapState.center.latitude+move[1]/111320,
                   longitude:mapState.center.longitude+move[0]/(111320*Math.cos(mapState.center.latitude*Math.PI/180))};
  renderMap();
});
$('map-save').onclick=async()=>{
  if(!mapState||!mapState.picked)return;
  const {latitude,longitude}=mapState.picked;
  if(await act('simulation_point_save',{latitude,longitude,name:$('map-point-name').value}))await reloadMap();
};

function connect(){if(api||!window.pywebview?.api)return;api=window.pywebview.api;refresh();}
window.addEventListener('pywebviewready',connect);
connect();
// Only the isolated preview server sets this query. Native production uses the bridge only.
if(new URLSearchParams(location.search).get('preview')==='1'){
  api={snapshot:async()=>{const r=await fetch('/api/state');if(!r.ok)throw Error();return r.json();},dispatch:async(action,payload)=>{const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,payload})});if(!r.ok)throw Error();return r.json();},simulation_map:async()=>{const r=await fetch('/api/simulation-map');if(!r.ok)throw Error();return r.json();}};
  refresh();
}
setInterval(refresh,1500);
setTimeout(()=>{if(!api)$('connection-error').hidden=false;},6000);
