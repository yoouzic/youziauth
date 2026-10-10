// Exercise the actual UI script with fictional snapshots, never a real backend.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

function updateFixture(overrides={}){
  // changes 默认缺席：老快照没有这一层，界面必须当没有它一样正常工作。
  return {state:'idle',current_version:'1.4.4',latest_version:'',progress:0,downloaded_bytes:0,total_bytes:0,checked:'',message:'等待后台检查更新。',busy:false,...overrides};
}
function accountsFixture(overrides={}){
  return {max:5,active:'a1',busy:false,error:'',items:[
    {id:'a1',name:'账号 1',active:true,has_idm_credentials:true,idm_username:'2023123456',missing:false,
     enabled:true,state:'waiting',message:'等待学校时段',busy:false,signed_today:false},
    {id:'a2',name:'账号 2',active:false,has_idm_credentials:false,idm_username:'',missing:false,
     enabled:false,state:'no_task',message:'今天暂无任务',busy:false,signed_today:false}],
    ...overrides};
}
function stubContext(){
  // The drawing path only issues canvas calls; a recorder-free stub lets the map tests run it.
  return {clearRect(){},fillRect(){},save(){},restore(){},beginPath(){},moveTo(){},lineTo(){},
          stroke(){},fill(){},arc(){},setLineDash(){},fillText(){},drawImage(){}};
}
function harness(source='windows',extras={}){
  const html=fs.readFileSync(path.join(__dirname,'../desktop_ui/index.html'),'utf8');
  const elements=new Map(),actions=[],navigation=[];
  for(const match of html.matchAll(/<(\w+)\b([^>]*)>/g)){
    const attributes=Object.fromEntries([...match[2].matchAll(/([\w-]+)="([^"]*)"/g)].map(a=>[a[1],a[2]]));
    const id=attributes.id,action=attributes['data-action'];
    const classes=new Set((attributes.class||'').split(/\s+/));
    if(!id&&!action&&!classes.has('nav-link'))continue;
    const element={
      id,tagName:match[1].toUpperCase(),value:attributes.value||'',
      // Read the attribute, not a bare default: index.html ships 在线底图 already checked.
      checked:/\schecked(?:\s|$)/.test(match[2]),
      hidden:/\shidden(?:\s|$)/.test(match[2]),disabled:/\sdisabled(?:\s|$)/.test(match[2]),open:false,
      textContent:'',firstChild:{textContent:''},dataset:action?{action}:{},attributes,hash:attributes.href||'',
      options:[],children:[],style:{},
      // 运行记录要把「用户滚到哪」当真：桩里给死数字，比在真浏览器里断言更稳。
      scrollTop:0,scrollHeight:900,clientHeight:500,
      scrollTo({top}){this.scrollTop=top;},
      appendChild(child){this.children.push(child);if(child.tagName==='OPTION')this.options.push(child);return child;},
      replaceChildren(...nodes){this.children=nodes;this.options=nodes.filter(node=>node.tagName==='OPTION');},
      set innerHTML(value){assert.fail('Backend text must never be rendered as HTML: '+value);},
      classList:{toggle(name,on){if(on)classes.add(name);else classes.delete(name);},contains(name){return classes.has(name);}},
      listeners:{},addEventListener(event,fn){this.listeners[event]=fn;},
      fire(event){this.listeners[event]?.({});},
      // 运行记录的提示胶囊按这个矩形贴到日志框下缘；桩里给个固定值就够断言位置被算过。
      getBoundingClientRect(){return {bottom:700,top:200,left:0,right:600,width:600,height:500};},
      setAttribute(name,value){this.attributes[name]=value;},removeAttribute(name){delete this.attributes[name];},
      focus(){},showModal(){this.open=true;},close(){this.open=false;this.listeners.close?.();},
    };
    if(id)elements.set(id,element);
    if(action)actions.push(element);
    if(classes.has('nav-link'))navigation.push(element);
  }
  const saveSource={id:'save-location-source',tagName:'BUTTON',disabled:false,textContent:'保存定位来源',
    listeners:{},addEventListener(event,fn){this.listeners[event]=fn;},focus(){},dataset:{}};
  elements.set('save-location-source',saveSource);
  for(const select of html.matchAll(/<select\b[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)){
    const element=elements.get(select[1]);
    element.options=[...select[2].matchAll(/<option\b[^>]*value="([^"]+)"[^>]*>([^<]*)<\/option>/g)].map(o=>({value:o[1],text:o[2]}));
    element.value=element.options[0]?.value||'';
  }
  const el=id=>{assert.ok(elements.has(id),'Missing UI node: '+id);return elements.get(id);};
  const canvas=elements.get('map-canvas');
  if(canvas)canvas.getContext=()=>stubContext();
  if(canvas&&extras.canvasRect)canvas.getBoundingClientRect=()=>extras.canvasRect;
  const stop=actions.find(button=>button.dataset.action==='network_stop');
  const data={preview:true,update:updateFixture(),location:{state:'idle',message:'尚未检测',accuracy:null,checked:'',busy:false},network:{username:'saved',interval:60,startup:false,monitoring:true,busy:false,state:'online',message:'online',checked:'',has_password:true},dorm:{state:'idle',message:'pending',busy:false,task:null,settings:{enabled:false,start:'21:00',end:'23:30',interval:300,location_source:source},schedule:'off'},logs:{network:extras.logs?.network||'',dorm:extras.logs?.dorm||''}};
  // 老快照没有账号层；accounts:false 就是那种机器，别的 extras 只覆盖清单里的字段。
  if(extras.accounts!==false)data.accounts={...accountsFixture(),...(extras.accounts||{})};
  // 每个账号在 state.dorm 里各有一份设置：切换后整份内容跟着换，测试靠这个差异证明表单真的换过来了。
  const dormSettings={a1:{...data.dorm.settings},
    a2:{enabled:true,start:'22:00',end:'23:00',interval:600,location_source:source}};
  const calls=[];
  const map={ok:true,point:{latitude:29.823693,longitude:106.422310,accuracy:100,source:'MAP_PICK',picked:true},
    reference:{latitude:29.823940,longitude:106.422470,address:'示例宿舍',radius_m:800},
    distance_m:31.6,in_range:true,
    points:[{id:'p1',name:'本机采样样本',latitude:29.823693,longitude:106.422310,accuracy:100,
             source:'SAMPLE',saved_at:'2026-09-24T09:40:00+08:00',active:true}],
    active_id:'p1',
    tile_url:'https://tile.openstreetmap.org/{z}/{x}/{y}.png',attribution:'© OpenStreetMap contributors',max_zoom:19,
    ...(extras.providers?{providers:extras.providers}:{})};
  const api={snapshot:async()=>structuredClone(data),dispatch:async(action,payload)=>{
    calls.push({action,payload});
    if(action==='dorm_save')Object.assign(data.dorm.settings,payload);
    if(action==='location_source_save')data.dorm.settings.location_source=payload.location_source;
    if(action==='simulation_point_save'){
      if(payload.latitude===null||typeof payload.latitude!=='number')return {ok:false,message:'选点坐标无效，请在地图上重新选择。'};
      const named=payload.name||'选点 2';
      const existing=map.points.find(point=>point.name===named);
      map.points=map.points.map(point=>({...point,active:false,
        ...(existing&&point.id===existing.id?{latitude:payload.latitude,longitude:payload.longitude}:{})}));
      if(existing){map.active_id=existing.id;}
      else{
        map.points.push({id:'p'+(map.points.length+1),name:named,latitude:payload.latitude,
                         longitude:payload.longitude,accuracy:100,source:'MAP_PICK',
                         saved_at:'2026-09-24T10:00:00+08:00',active:true});
        map.active_id=map.points[map.points.length-1].id;
      }
      const chosen=map.points.find(point=>point.id===map.active_id);
      map.point={latitude:chosen.latitude,longitude:chosen.longitude,accuracy:100,source:'MAP_PICK',picked:true};
    }
    if(action==='simulation_point_select'){
      map.points=map.points.map(point=>({...point,active:point.id===payload.id}));
      const chosen=map.points.find(point=>point.active);
      if(!chosen)return {ok:false,message:'找不到这个选点，请刷新后重试。'};
      map.active_id=chosen.id;
      map.point={latitude:chosen.latitude,longitude:chosen.longitude,accuracy:100,
                 source:chosen.source,picked:true};
    }
    if(action==='simulation_point_rename'){
      if(!payload.name)return {ok:false,message:'名称不能为空，最多 24 个字。'};
      map.points=map.points.map(point=>point.id===payload.id?{...point,name:payload.name}:point);
    }
    if(action==='simulation_point_delete'){
      map.points=map.points.filter(point=>point.id!==payload.id);
      if(map.active_id===payload.id){
        map.active_id=map.points.length?map.points[0].id:'';
        map.points=map.points.map(point=>({...point,active:point.id===map.active_id}));
        const chosen=map.points.find(point=>point.active);
        if(chosen)map.point={latitude:chosen.latitude,longitude:chosen.longitude,accuracy:100,
                             source:chosen.source,picked:true};
      }
    }
    if(action==='account_add'){
      const items=data.accounts.items,id='a'+(items.length+1);
      items.forEach(item=>{item.active=false;});
      items.push({id,name:payload.name||`账号 ${items.length+1}`,active:true,
                  has_idm_credentials:false,idm_username:'',missing:false});
      data.accounts.active=id;
      data.dorm.settings={enabled:false,start:'21:00',end:'23:30',interval:300,
                          location_source:data.dorm.settings.location_source};
    }
    if(action==='account_switch'){
      const target=data.accounts.items.find(item=>item.id===payload.id);
      if(!target)return {ok:false,message:'找不到这个账号，请刷新后重试。'};
      if(target.missing)return {ok:false,message:`账号「${target.name}」的本机档案目录不存在，无法切换。`};
      data.accounts.items.forEach(item=>{item.active=item.id===payload.id;});
      data.accounts.active=payload.id;
      data.dorm.settings={...(dormSettings[payload.id]||dormSettings.a1)};
    }
    if(action==='account_rename'){
      if(!payload.name)return {ok:false,message:'请填写账号名称，最多 24 个字。'};
      const clean=payload.name.trim().slice(0,24);
      data.accounts.items.forEach(item=>{if(item.id===payload.id)item.name=clean;});
    }
    if(action==='account_delete'){
      if(payload.confirmed!==true)return {ok:false,message:'删除账号需要确认。'};
      data.accounts.items=data.accounts.items.filter(item=>item.id!==payload.id);
      if(data.accounts.active===payload.id)data.accounts.active=data.accounts.items[0]?.id||'';
      data.accounts.items.forEach(item=>{item.active=item.id===data.accounts.active;});
      if(data.accounts.active)data.dorm.settings={...(dormSettings[data.accounts.active]||dormSettings.a1)};
    }
    return {ok:true,message:'操作完成'};
  },simulation_map:async()=>structuredClone(map)};
  const context=vm.createContext({console,URLSearchParams,Intl,Date,Promise,
    location:{hash:'#network',search:''},setInterval(){},
    // Tile loading needs globals the real window has but the sandbox does not: without them the
    // picker must still work, which is exactly what the untouched tests keep proving.
    setTimeout:extras.setTimeout||function(){},clearTimeout:extras.clearTimeout||function(){},
    Image:extras.Image,getComputedStyle:extras.getComputedStyle,
    window:{addEventListener(){},scrollTo(){},pywebview:{api}},
    document:{getElementById:id=>elements.get(id)||null,
      createElement(tag){return {tagName:String(tag).toUpperCase(),value:'',textContent:'',selected:false,
                                href:'',target:'',rel:''};},
      querySelectorAll:selector=>{
      if(selector==='[data-action]')return actions;
      if(selector==='.nav-link')return navigation;
      // 动作名要按整串匹配：'check-updates' 的子串会命中 'install-update' 之类，
      // 那样一个按钮会被当成另一个按钮，禁用状态看起来「没生效」。
      const selected=[...selector.matchAll(/\[data-action="([^"]+)"\]/g)].map(m=>m[1]);
      if(selected.length)return actions.filter(button=>selected.includes(button.dataset.action));
      // id 选择器同样按整串匹配，避免 #check 命中 #check-updates 这类前缀重合。
      const ids=[...selector.matchAll(/#([\w-]+)/g)].map(m=>m[1]);
      return ids.map(id=>elements.get(id)).filter(Boolean);
    }},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../desktop_ui/app.js'),'utf8'),context);
  return {el,data,api,map,calls,context,stop,navigation,refresh:()=>vm.runInContext('refresh()',context),
    // 日志滚动的状态在 app.js 里，测试只读地检查它，不去碰界面逻辑。
    logView:()=>vm.runInContext('logView',context),
    // 用户滚动：位置和滚动事件要一起发生，否则 1.5 秒心跳那边看到的还是旧位置。
    scrolled(top){const log=el('log-output');log.scrollTop=top;log.fire('scroll');},
    navigate(page){context.location.hash='#'+page;vm.runInContext('navigate()',context);}};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function pickSource(h,source){h.el('location-source').value=source;h.el('location-source').listeners.change();}function saveSource(h){return h.el('save-location-source').listeners.click();}
function saveSchedule(h){return h.el('dorm-form').listeners.submit({preventDefault(){}});}

test('unsaved network form does not block stopping an active monitor',async()=>{
  const h=harness();await settle();
  h.el('network-form').listeners.input();
  h.stop.listeners.click();await settle();
  assert.equal(h.calls[0]?.action,'network_stop');
});

test('an edit made during network save remains dirty and is not replaced',async()=>{
  const h=harness();await settle();
  let complete;h.api.dispatch=()=>new Promise(resolve=>complete=resolve);
  h.el('username').value='submitted';h.el('password').value='first';
  h.el('network-form').listeners.input();
  const saving=h.el('network-form').listeners.submit({preventDefault(){}});
  h.el('username').value='later-edit';h.el('password').value='later-password';
  h.el('network-form').listeners.input();
  complete({ok:true,message:'saved'});await saving;
  assert.equal(h.el('username').value,'later-edit');
  assert.equal(h.el('password').value,'later-password');
  assert.equal(h.el('network-save-state').textContent,'有未保存的修改');
});

test('an edit made during dorm save remains dirty',async()=>{
  const h=harness();await settle();
  let complete;h.api.dispatch=()=>new Promise(resolve=>complete=resolve);
  h.el('dorm-form').listeners.input();
  const saving=h.el('dorm-form').listeners.submit({preventDefault(){}});
  h.el('auto-interval').value='600';h.el('dorm-form').listeners.input();
  complete({ok:true,message:'saved'});await saving;
  assert.equal(h.el('auto-interval').value,'600');
  assert.equal(h.el('dorm-save-state').textContent,'有未保存的修改');
});

test('failed save preserves the draft and password',async()=>{
  const h=harness();await settle();
  h.api.dispatch=async()=>({ok:false,message:'failed'});
  h.el('username').value='draft';h.el('password').value='draft-password';
  h.el('network-form').listeners.input();
  await h.el('network-form').listeners.submit({preventDefault(){}});
  assert.equal(h.el('username').value,'draft');
  assert.equal(h.el('password').value,'draft-password');
  assert.equal(h.el('network-save-state').textContent,'有未保存的修改');
});

test('a successful network save without a snapshot preserves the draft and password',async()=>{
  const h=harness();await settle();
  h.api.snapshot=async()=>{throw Error('snapshot unavailable');};
  h.el('username').value='draft';h.el('password').value='draft-password';
  h.el('network-form').listeners.input();
  await h.el('network-form').listeners.submit({preventDefault(){}});
  assert.equal(h.el('username').value,'draft');
  assert.equal(h.el('password').value,'draft-password');
  assert.equal(h.el('network-save-state').textContent,'有未保存的修改');
  assert.match(h.el('toast').textContent,/操作已执行.*尚未同步/);
});

test('a startup change warns that Windows will ask for administrator approval',async()=>{
  const h=harness();await settle();
  let complete;
  h.api.dispatch=(action,payload)=>new Promise(resolve=>complete=()=>resolve({ok:true,message:'操作完成'}));
  h.el('startup').checked=true;
  const saving=h.el('network-form').listeners.submit({preventDefault(){}});
  assert.match(h.el('toast').textContent,/管理员授权/);
  complete();await saving;
});

test('saving without a startup change does not claim a UAC prompt',async()=>{
  const h=harness();await settle();
  h.el('username').value='student';
  h.el('network-form').listeners.input();
  await h.el('network-form').listeners.submit({preventDefault(){}});
  assert.doesNotMatch(h.el('toast').textContent,/管理员授权/);
});

test('location actions wait for the first snapshot',async()=>{
  const h=harness();
  h.el('authorize-location').listeners.click();h.el('submit-dorm').onclick();
  assert.equal(h.calls.length,0);
  assert.equal(h.el('confirm-dialog').open,false);
  assert.match(h.el('toast').textContent,/尚未同步/);
  await settle();
  h.el('authorize-location').listeners.click();await settle();
  assert.deepEqual(h.calls.map(call=>call.action),['location_authorize']);
});

test('native location select restores the saved source without performing actions',async()=>{
  const h=harness('simulation');await settle();
  const select=h.el('location-source');
  assert.equal(select.tagName,'SELECT');
  assert.deepEqual(select.options,[{value:'windows',text:'真实定位（Windows / Wi-Fi）'},{value:'simulation',text:'模拟定位'}]);
  assert.equal(select.value,'simulation');
  assert.equal(h.calls.length,0);
  h.data.dorm.settings.location_source='windows';await h.refresh();
  assert.equal(select.value,'windows');
});

test('saving the source sends only the source and keeps the schedule untouched',async()=>{
  const h=harness();await settle();
  pickSource(h,'simulation');await saveSource(h);
  assert.equal(h.calls.length,1);
  assert.equal(h.calls[0].action,'location_source_save');
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0].payload)),{location_source:'simulation'});
  assert.equal(h.el('location-source').value,'simulation');
  assert.equal(h.el('dorm-save-state').textContent,'设置已同步');
  assert.match(h.el('overview-location-source').textContent,/模拟/);
  assert.match(h.el('overview-location-source').textContent,/模拟/);
});

test('saving the schedule alone never rewrites the saved location source',async()=>{
  const h=harness('simulation');await settle();
  h.el('auto-interval').value='600';h.el('dorm-form').listeners.input();
  await saveSchedule(h);
  assert.equal(h.calls.length,1);
  assert.equal(h.calls[0].action,'dorm_save');
  assert.equal('location_source' in h.calls[0].payload,false);
  assert.equal(h.data.dorm.settings.location_source,'simulation');
  assert.match(h.el('overview-location-source').textContent,/模拟/);
});

test('the source save button stays idle until the draft differs from the saved source',async()=>{
  const h=harness();await settle();
  assert.equal(h.el('save-location-source').disabled,true);
  pickSource(h,'simulation');await h.refresh();
  assert.equal(h.el('save-location-source').disabled,false);
  assert.equal(h.el('dorm-save-state').textContent,'有未保存的修改');
  pickSource(h,'windows');await h.refresh();
  assert.equal(h.el('save-location-source').disabled,true);
});
test('refresh preserves a source draft but labels and detection use the saved source',async()=>{
  const h=harness();await settle();
  pickSource(h,'simulation');await h.refresh();
  assert.equal(h.el('location-source').value,'simulation');
  assert.match(h.el('overview-location-source').textContent,/真实.*Windows/);
  assert.equal(h.el('authorize-location').textContent,'授权并检测定位');
  assert.equal(h.calls.length,0);
});

test('a source edit during save survives and the accepted source drives the label',async()=>{
  const h=harness();await settle();
  let complete;
  h.api.dispatch=(action,payload)=>new Promise(resolve=>{complete=()=>{h.data.dorm.settings.location_source=payload.location_source;resolve({ok:true,message:'saved'});};});
  pickSource(h,'simulation');const saving=saveSource(h);
  pickSource(h,'windows');complete();await saving;
  assert.equal(h.el('location-source').value,'windows');
  assert.equal(h.el('dorm-save-state').textContent,'有未保存的修改');
  assert.match(h.el('overview-location-source').textContent,/模拟/);
  h.el('authorize-location').listeners.click();await settle();
  assert.match(h.el('toast').textContent,/先保存.*来源/);
});

test('failed source save keeps the draft while the saved source remains real',async()=>{
  const h=harness();await settle();
  h.api.dispatch=async()=>({ok:false,message:'failed'});
  pickSource(h,'simulation');await saveSource(h);
  assert.equal(h.el('location-source').value,'simulation');
  assert.equal(h.el('dorm-save-state').textContent,'有未保存的修改');
  assert.match(h.el('overview-location-source').textContent,/真实/);
});

test('saved simulation stays dirty and blocks location actions until its snapshot recovers',async()=>{
  const h=harness();await settle();
  h.data.dorm.state='ready';await h.refresh();
  const snapshot=h.api.snapshot;
  h.api.snapshot=async()=>{throw Error('snapshot unavailable');};
  pickSource(h,'simulation');await saveSource(h);
  assert.deepEqual(h.calls.map(call=>call.action),['location_source_save']);
  assert.equal(h.el('location-source').value,'simulation');
  assert.equal(h.el('dorm-save-state').textContent,'有未保存的修改');
  assert.equal(h.el('connection-error').hidden,false);
  assert.match(h.el('toast').textContent,/操作已执行.*尚未同步/);
  h.el('authorize-location').listeners.click();h.el('submit-dorm').onclick();await settle();
  assert.deepEqual(h.calls.map(call=>call.action),['location_source_save']);
  assert.equal(h.el('confirm-dialog').open,false);
  assert.equal(h.el('authorize-location').disabled,true);
  assert.equal(h.el('submit-dorm').disabled,true);
  assert.match(h.el('toast').textContent,/尚未同步/);

  h.api.snapshot=snapshot;await h.refresh();
  assert.equal(h.el('connection-error').hidden,true);
  assert.equal(h.el('location-source').value,'simulation');
  assert.match(h.el('overview-location-source').textContent,/模拟/);
  assert.equal(h.el('authorize-location').disabled,false);
  assert.equal(h.el('submit-dorm').disabled,false);
  assert.deepEqual(h.calls.map(call=>call.action),['location_source_save']);
  h.el('authorize-location').listeners.click();await settle();
  h.el('submit-dorm').onclick();
  assert.match(h.el('confirm-text').textContent,/已保存的模拟位置/);
  h.el('confirm-ok').onclick();await settle();
  assert.deepEqual(h.calls.map(call=>call.action),['location_source_save','location_authorize','dorm_submit']);
});

test('a failed snapshot blocks even a matching source and recovery reads the real saved source',async()=>{
  const h=harness();await settle();
  h.data.dorm.state='ready';await h.refresh();
  const snapshot=h.api.snapshot;
  h.api.snapshot=async()=>{throw Error('snapshot unavailable');};
  await h.refresh();
  h.el('authorize-location').listeners.click();h.el('submit-dorm').onclick();await settle();
  assert.equal(h.calls.length,0);
  assert.equal(h.el('confirm-dialog').open,false);
  assert.equal(h.el('authorize-location').disabled,true);
  assert.equal(h.el('submit-dorm').disabled,true);
  assert.equal(h.el('connection-error').hidden,false);
  assert.match(h.el('toast').textContent,/尚未同步/);

  h.data.dorm.settings.location_source='simulation';h.api.snapshot=snapshot;await h.refresh();
  assert.equal(h.el('location-source').value,'simulation');
  assert.match(h.el('overview-location-source').textContent,/模拟/);
  assert.equal(h.el('connection-error').hidden,true);
  assert.equal(h.el('authorize-location').disabled,false);
  assert.equal(h.el('submit-dorm').disabled,false);
});

test('simulation detection is clearly non-live and requires no Windows permission',async()=>{
  const h=harness('simulation');await settle();
  assert.equal(h.el('authorize-location').textContent,'检测模拟定位');
  // 概览行的定位来源只写来源本身，不再重复「（已保存样本）」这句说明。
  assert.equal(h.el('overview-location-source').textContent,'模拟定位');
  // 定位来源卡片只保留选择器、保存按钮与检测框：说明行与来源徽标已删除。
  const markup=fs.readFileSync(path.join(__dirname,'../desktop_ui/index.html'),'utf8');
  assert.doesNotMatch(markup,/location-source-badge|location-source-scope/);
  // 冗长的模拟定位说明已移除：模拟模式下不再显示任何模式说明文字。
  assert.equal(h.el('location-mode-hint').hidden,true);
  h.el('authorize-location').listeners.click();await settle();
  assert.equal(h.calls[0]?.action,'location_authorize');
  assert.match(h.el('toast').textContent,/模拟.*非实时/);
  h.data.location={state:'checking',message:'检测中',accuracy:null,checked:'',busy:true};await h.refresh();
  assert.equal(h.el('authorize-location').disabled,true);
  assert.match(h.el('authorize-location').textContent,/检测模拟定位/);
  assert.doesNotMatch(h.el('authorize-location').textContent,/授权/);
  h.data.location={state:'ready',message:'检测通过',accuracy:25,checked:'21:10',busy:false};await h.refresh();
  assert.equal(h.el('authorize-location').disabled,false);
  assert.match(h.el('location-message').textContent,/模拟.*非实时/);
  assert.match(h.el('location-checked').textContent,/模拟.*21:10/);
});

test('saving Windows restores real detection and explains Windows chooses the source',async()=>{
  const h=harness('simulation');await settle();
  pickSource(h,'windows');await saveSource(h);
  assert.equal(h.calls[0].action,'location_source_save');
  assert.equal(h.calls[0].payload.location_source,'windows');
  assert.equal(h.el('authorize-location').textContent,'授权并检测定位');
  assert.match(h.el('overview-location-source').textContent,/真实.*Windows/);
  assert.doesNotMatch(h.el('overview-location-source').textContent,/模拟/);
  assert.match(h.el('location-mode-hint').textContent,/Windows.*决定/);
  assert.match(h.el('location-mode-hint').textContent,/不保证.*Wi-Fi/);
  assert.equal(h.el('location-mode-hint').hidden,false);
});

test('the removed verbose location paragraphs stay out of the markup',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../desktop_ui/index.html'),'utf8');
  assert.doesNotMatch(html,/location-submit-hint/);
  assert.doesNotMatch(html,/自动执行需电脑开机/);
  assert.doesNotMatch(html,/随机偏移/);
  assert.doesNotMatch(html,/已保存的模拟位置（非实时）/);
});

test('the removed page hints stay out of the markup',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../desktop_ui/index.html'),'utf8');
  // 依据用户标注逐页删掉的长说明：运行记录分享提醒、校园网托盘说明、寝室打卡静默登录说明、软件更新图标与两段安装说明。
  assert.doesNotMatch(html,/向他人分享记录前/);
  assert.doesNotMatch(html,/关闭窗口会收到系统托盘/);
  assert.doesNotMatch(html,/保存后，登录时会自动点/);
  assert.doesNotMatch(html,/发现新正式版后自动下载安装包/);
  assert.doesNotMatch(html,/确认后将重新验证安装包并打开安装向导/);
  assert.doesNotMatch(html,/section-mark"><i class="bi bi-arrow-clockwise"/);
  // 「（已保存样本）」不再出现在下拉选项里，来源名称本身就是完整文案。
  assert.doesNotMatch(html,/模拟定位（已保存样本）/);
});

for(const saved of ['windows','simulation']){
  test(`unsaved source blocks detection and submission when saved source is ${saved}`,async()=>{
    const h=harness(saved);await settle();
    h.data.dorm.state='ready';await h.refresh();
    pickSource(h,saved==='windows'?'simulation':'windows');
    h.el('authorize-location').listeners.click();await settle();
    assert.equal(h.calls.length,0);
    assert.match(h.el('toast').textContent,/先保存.*来源/);
    h.el('toast').textContent='';h.el('submit-dorm').onclick();
    assert.equal(h.el('confirm-dialog').open,false);
    assert.equal(h.calls.length,0);
    assert.match(h.el('toast').textContent,/先保存.*来源/);
  });
}

test('unsaved schedule edits or a reverted source do not block detection or submission',async()=>{
  const h=harness();await settle();
  h.data.dorm.state='ready';await h.refresh();
  h.el('auto-interval').value='600';h.el('dorm-form').listeners.input();
  pickSource(h,'simulation');pickSource(h,'windows');
  h.el('authorize-location').listeners.click();await settle();
  assert.equal(h.calls[0]?.action,'location_authorize');
  h.el('submit-dorm').onclick();assert.equal(h.el('confirm-dialog').open,true);
  h.el('confirm-ok').onclick();await settle();
  assert.equal(h.calls[1]?.action,'dorm_submit');
});

for(const source of ['windows','simulation']){
  test(`${source} submission confirmation describes the saved source sent to school`,async()=>{
    const h=harness(source);await settle();
    h.data.dorm.state='ready';await h.refresh();
    h.el('submit-dorm').onclick();
    assert.equal(h.el('confirm-dialog').open,true);
    const text=h.el('confirm-text').textContent;
    assert.match(text,/向学校发送/);
    if(source==='simulation'){
      assert.match(text,/已保存的模拟位置/);assert.match(text,/非实时/);
      assert.doesNotMatch(text,/Windows 实时定位/);
    }else assert.match(text,/Windows 实时定位/);
    assert.equal(h.calls.length,0);
    h.el('confirm-ok').onclick();await settle();
    assert.equal(h.calls[0]?.action,'dorm_submit');
    if(source==='simulation')assert.match(h.el('toast').textContent,/模拟.*非实时/);
  });

  test(`${source} uncertain confirmation only queries an existing result`,async()=>{
    const h=harness(source);await settle();
    h.data.dorm.state='uncertain';await h.refresh();
    h.el('submit-dorm').onclick();
    assert.equal(h.el('confirm-title').textContent,'回查提交结果？');
    const text=h.el('confirm-text').textContent;
    assert.match(text,/仅.*查询.*已有.*结果/);
    assert.doesNotMatch(text,/将使用|实时定位|向学校发送.*位置/);
    h.el('confirm-ok').onclick();await settle();
    assert.deepEqual(h.calls.map(call=>call.action),['dorm_query']);
  });
}

test('uncertain confirmation only queries when the server has already replaced the task',async()=>{
  const h=harness();await settle();
  h.data.dorm.state='uncertain';
  h.data.dorm.task={title:'旧任务',date:'2026-09-01',start:'21:00',end:'23:30',address:'测试宿舍'};
  await h.refresh();h.el('submit-dorm').onclick();
  h.data.dorm.state='ready';
  h.data.dorm.task={title:'新任务',date:'2026-09-02',start:'21:00',end:'23:30',address:'测试宿舍'};
  assert.equal(h.el('task-title').textContent,'旧任务');
  h.el('confirm-ok').onclick();await settle();
  assert.deepEqual(h.calls.map(call=>call.action),['dorm_query']);
});

test('confirmation cannot submit a new unsaved source draft',async()=>{
  const h=harness();await settle();
  h.data.dorm.state='ready';await h.refresh();h.el('submit-dorm').onclick();
  pickSource(h,'simulation');h.el('confirm-ok').onclick();await settle();
  assert.equal(h.calls.length,0);
  assert.match(h.el('toast').textContent,/先保存.*来源/);
});

test('confirmation cannot silently use a different saved source',async()=>{
  const h=harness();await settle();
  h.data.dorm.state='ready';await h.refresh();h.el('submit-dorm').onclick();
  h.data.dorm.settings.location_source='simulation';await h.refresh();
  h.el('confirm-ok').onclick();await settle();
  assert.equal(h.calls.length,0);
  assert.match(h.el('toast').textContent,/重新确认/);
});

test('an uncertain confirmation cannot turn into a new submission after refresh',async()=>{
  const h=harness();await settle();
  h.data.dorm.state='uncertain';await h.refresh();h.el('submit-dorm').onclick();
  h.data.dorm.state='ready';await h.refresh();
  h.el('confirm-ok').onclick();await settle();
  assert.equal(h.calls.length,0);
  assert.match(h.el('toast').textContent,/重新确认/);
});

for(const state of ['ready','uncertain']){
  test(`${state} confirmation cannot execute after a snapshot failure`,async()=>{
    const h=harness();await settle();
    h.data.dorm.state=state;await h.refresh();h.el('submit-dorm').onclick();
    assert.equal(h.el('confirm-dialog').open,true);
    h.api.snapshot=async()=>{throw Error('snapshot unavailable');};
    await h.refresh();h.el('confirm-ok').onclick();await settle();
    assert.equal(h.calls.length,0);
    assert.match(h.el('toast').textContent,/尚未同步/);
  });
}

test('a stale snapshot cannot unlock location actions while the current snapshot is pending',async()=>{
  const h=harness();await settle();
  h.data.dorm.state='ready';await h.refresh();
  let completeOld,completeCurrent;
  h.api.snapshot=()=>new Promise(resolve=>completeOld=resolve);
  const refreshing=h.refresh();
  h.stop.listeners.click();await settle();
  h.api.snapshot=()=>new Promise(resolve=>completeCurrent=resolve);
  completeOld(structuredClone(h.data));await refreshing;await settle();
  h.el('authorize-location').listeners.click();h.el('submit-dorm').onclick();await settle();
  assert.deepEqual(h.calls.map(call=>call.action),['network_stop']);
  assert.equal(h.el('confirm-dialog').open,false);
  assert.match(h.el('toast').textContent,/尚未同步/);
  completeCurrent(structuredClone(h.data));await settle();
  assert.equal(h.el('authorize-location').disabled,false);
  assert.equal(h.el('submit-dorm').disabled,false);
});

test('a stale snapshot rejection does not change the current connection status',async()=>{
  const h=harness();await settle();
  const snapshot=h.api.snapshot;
  let rejectOld,completeAction;
  h.api.snapshot=()=>new Promise((resolve,reject)=>rejectOld=reject);
  const refreshing=h.refresh();
  h.api.dispatch=async(action,payload)=>{
    h.calls.push({action,payload});await new Promise(resolve=>completeAction=resolve);
    return {ok:true,message:'操作完成'};
  };
  h.stop.listeners.click();
  rejectOld(Error('obsolete snapshot failed'));await refreshing;
  assert.equal(h.el('connection-error').hidden,true);
  h.api.snapshot=snapshot;completeAction();await settle();
  assert.equal(h.el('connection-error').hidden,true);
  assert.equal(h.el('authorize-location').disabled,false);
});

test('a snapshot obtained during dispatch cannot authorize confirmation after dispatch completes',async()=>{
  const h=harness();await settle();
  h.data.dorm.state='ready';await h.refresh();h.el('submit-dorm').onclick();
  let completeAction,completeSnapshot;
  h.api.dispatch=async(action,payload)=>{
    h.calls.push({action,payload});
    if(action==='network_stop')await new Promise(resolve=>completeAction=resolve);
    return {ok:true,message:'操作完成'};
  };
  h.stop.listeners.click();await h.refresh();
  h.api.snapshot=()=>new Promise(resolve=>completeSnapshot=resolve);
  completeAction();await settle();
  h.el('confirm-ok').onclick();await settle();
  assert.deepEqual(h.calls.map(call=>call.action),['network_stop']);
  assert.match(h.el('toast').textContent,/尚未同步/);
  completeSnapshot(structuredClone(h.data));await settle();
  assert.equal(h.el('submit-dorm').disabled,false);
});

async function updateHarness(overrides={}){
  const h=harness();await settle();
  h.data.update=updateFixture(overrides);await h.refresh();
  return h;
}
const readyUpdate={state:'ready',latest_version:'1.5.0',progress:100,downloaded_bytes:10485760,total_bytes:10485760,checked:'2026-09-15 09:30',message:'安装包已下载，哈希与签名验证通过。'};
// 「更新了什么」块，字段与 app_update.read_changes() 完全一致。
function changesFixture(overrides={}){
  return {entries:[{subject:'choose, name and manage the simulated position on a map',kind:'feat',date:'2026-09-24'},
                   {subject:'stop reporting a stale portal session as a healthy uplink',kind:'fix',date:'2026-10-10'}],
          total:2,more:false,note:'',...overrides};
}
// 发布者手写的中文说明：kind 是中文标签，date 为空（后端不给手写说明编日期）。
function chineseChangesFixture(overrides={}){
  return {entries:[
    {subject:'校园网：代理开着时不再误报「能上网」',kind:'',date:''},
    {subject:'寝室打卡支持每个账号独立时段',kind:'重点',date:''},
    {subject:'定位来源切换后立刻对自动打卡生效',kind:'',date:''}],
    total:3,more:false,note:'这一版只改校园网和打卡。',...overrides};
}
function manyEntries(count){
  return Array.from({length:count},(unused,index)=>({subject:'fix number '+index,kind:'fix',date:'2026-10-10'}));
}

function clickUpdate(h,id){
  const button=h.el(id);
  assert.equal(typeof button.onclick,'function','Update buttons need a guarded handler');
  button.onclick();
}

test('updates navigation selects its own page and retains the sidebar link',async()=>{
  const h=await updateHarness();h.navigate('updates');
  assert.equal(h.el('page-context').textContent,'软件更新');
  assert.equal(h.el('updates').hidden,false);
  assert.equal(h.el('network').hidden,true);
  const link=h.navigation.find(link=>link.hash==='#updates');
  assert.ok(link,'Software updates must be reachable from navigation');
  assert.equal(link.attributes['aria-current'],'page');
});

test('update actions are disabled and cannot run before the first snapshot',async()=>{
  const h=harness();
  assert.equal(h.el('check-updates').disabled,true);
  // 按钮在首次快照前是禁用的，处理器自己也挡住 —— 即便有人直接调用也不发请求。
  clickUpdate(h,'check-updates');
  assert.equal(h.calls.length,0);
  await settle();
});

test('the interface has no install action: the privileged agent installs',async()=>{
  // 关键安全性质的前端一侧：界面里根本不存在「安装更新」按钮，也没有确认对话框
  // 那条路径，所以未提权的界面不可能触发安装。
  const html=fs.readFileSync(path.join(__dirname,'../desktop_ui/index.html'),'utf8');
  assert.doesNotMatch(html,/install-update/);
  const source=fs.readFileSync(path.join(__dirname,'../desktop_ui/app.js'),'utf8');
  assert.doesNotMatch(source,/update_install/);
  assert.doesNotMatch(source,/install-update/);
});

test('idle updates show current version and offer an explicit check without auto-dispatch',async()=>{
  const h=await updateHarness();
  assert.equal(h.el('update-current-version').textContent,'1.4.4');
  assert.match(h.el('update-latest-version').textContent,/尚未检查/);
  assert.match(h.el('update-status').textContent,/等待检查/);
  assert.equal(h.el('update-message').textContent,'等待后台检查更新。');
  assert.match(h.el('update-checked').textContent,/尚未检查/);
  assert.equal(h.el('check-updates').disabled,false);
  assert.equal(h.el('update-notice').hidden,true);
  assert.equal(h.calls.length,0);
  clickUpdate(h,'check-updates');await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)),[{action:'update_check',payload:{}}]);
});

test('what changed is readable while the package is still downloading',async()=>{
  // 说明是在下载安装包**之前**取的，所以下载中就该看得到。等几十 MB 下完才显示，
  // 等于让用户在整个下载期间都不知道自己在等什么。
  const notes={'entries':[{'subject':'校园网：不再误报「能上网」','kind':'','date':''},
                          {'subject':'更新页会写清这次改了什么','kind':'重点','date':''}],
               'total':2,'more':false,'note':''};
  for(const [state,label] of [['downloading','正在后台下载'],['verifying','正在验证'],
                              ['ready','准备安装'],['installing','正在后台安装']]){
    const h=await updateHarness({...readyUpdate,state,changes:notes,
      downloaded_bytes:4194304,total_bytes:10485760,progress:40});
    assert.equal(h.el('update-status').textContent,label,'state '+state);
    assert.equal(h.el('update-changes').hidden,false,'notes must be visible in '+state);
    assert.equal(h.el('update-changes-list').children.length,2,'notes must be listed in '+state);
    assert.equal(h.el('update-changes-summary').textContent,'1.4.4 → v1.5.0 · 共 2 条');
    assert.equal(h.el('update-changes-list').children[1].textContent,'重点 · 更新页会写清这次改了什么');
  }
  // 而「已经是最新」时不该出现：没有新版本就没有「这次改了什么」。
  const fresh=await updateHarness({state:'up_to_date',latest_version:'1.4.4',changes:notes});
  assert.equal(fresh.el('update-changes').hidden,true);
});

test('the check button asks the agent to look, it never installs',async()=>{
  // 用「已是最新」：'ready' 是代理准备安装，检查按钮本身就应该是禁用的。
  const h=await updateHarness({state:'up_to_date',latest_version:'1.4.4',message:'当前已是最新正式版。'});
  assert.equal(h.el('check-updates').disabled,false);
  clickUpdate(h,'check-updates');await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)),[{action:'update_check',payload:{}}]);
  assert.equal(h.calls.some(call=>call.action==='update_install'),false);
});

test('up-to-date state never offers a downgrade when the release is older',async()=>{
  const h=await updateHarness({state:'up_to_date',latest_version:'1.4.3',checked:'2026-09-15 09:30',message:'当前版本已是最新，无需更新。'});
  assert.equal(h.el('update-latest-version').textContent,'1.4.3');
  assert.match(h.el('update-status').textContent,/无需更新/);
  assert.match(h.el('update-message').textContent,/无需更新/);
  assert.match(h.el('update-checked').textContent,/2026-09-15 09:30/);
  assert.equal(h.el('update-notice').hidden,true);
  assert.equal(h.calls.length,0);
});

test('update errors persist across pages without poll toasts and allow a retry',async()=>{
  const message='GitHub 下载失败：连接超时，请重新检查后重试。';
  const h=await updateHarness({state:'error',message});
  assert.match(h.el('update-status').textContent,/更新未完成/);
  assert.equal(h.el('update-message').textContent,message);
  assert.equal(h.el('check-updates').textContent,'再检查一次');
  assert.equal(h.el('check-updates').disabled,false);
  for(const page of ['overview','network','dorm','records','updates']){
    h.navigate(page);await h.refresh();
    assert.equal(h.el('update-notice').hidden,false);
    assert.equal(h.el('update-notice').classList.contains('error'),true);
    assert.ok(h.el('update-notice-text').textContent.includes(message));
    assert.equal(h.el('update-notice-link').attributes.href,'#updates');
    assert.equal(h.el('toast').hidden,true);
  }
  clickUpdate(h,'check-updates');await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)),[{action:'update_check',payload:{}}]);
  h.data.update=updateFixture({state:'checking',busy:true});await h.refresh();
  assert.equal(h.el('update-notice').hidden,true);
});

for(const [state,label] of [['checking','正在检查'],['downloading','正在后台下载'],['verifying','正在验证'],
                            ['installing','正在后台安装'],['ready','准备安装'],['installed','已自动更新']]){
  test(`${state} updates are reported without asking the user to install`,async()=>{
    const h=await updateHarness({...readyUpdate,state,message:'正在处理，请稍候。'});
    assert.equal(h.el('update-status').textContent,label);
    if(state==='installed'){
      assert.equal(h.el('check-updates').disabled,false);
      assert.equal(h.el('update-notice').hidden,false);
      assert.match(h.el('update-notice-text').textContent,/已自动更新到 1\.5\.0/);
    }else{
      assert.equal(h.el('check-updates').disabled,true);
    }
    clickUpdate(h,'check-updates');await settle();
    assert.deepEqual(h.calls.map(call=>call.action).filter(a=>a==='update_install'),[]);
  });
}

test('download progress exposes percentage and transferred size with an accessible progress element',async()=>{
  const h=await updateHarness({state:'downloading',busy:true,latest_version:'1.5.0',progress:40,downloaded_bytes:4194304,total_bytes:10485760});
  const progress=h.el('update-progress');
  assert.equal(progress.tagName,'PROGRESS');
  assert.equal(progress.attributes.max,'100');
  assert.equal(progress.attributes['aria-labelledby'],'update-progress-label');
  assert.equal(progress.hidden,false);
  assert.equal(progress.value,40);
  assert.match(h.el('update-progress-label').textContent,/40%/);
  assert.equal(h.el('update-download-size').textContent,'4.0 MiB / 10.0 MiB');
  h.data.update.downloaded_bytes=512;h.data.update.total_bytes=0;await h.refresh();
  assert.match(h.el('update-download-size').textContent,/512 B.*总大小未知/);
});

test('a silent install reports progress and then success without a confirmation step',async()=>{
  const h=await updateHarness({...readyUpdate,state:'installing',message:'正在后台静默安装 v1.5.0…'});
  assert.equal(h.el('update-status').textContent,'正在后台安装');
  assert.equal(h.el('update-progress').hidden,false);
  assert.equal(h.el('check-updates').disabled,true);
  assert.equal(h.el('confirm-dialog').open,false);
  h.data.update=updateFixture({state:'installed',latest_version:'1.5.0',progress:100,
    downloaded_bytes:10485760,total_bytes:10485760,message:'已自动更新到 v1.5.0。',
    current_version:'1.5.0'});await h.refresh();
  assert.equal(h.el('update-status').textContent,'已自动更新');
  assert.equal(h.el('update-current-version').textContent,'1.5.0');
  assert.equal(h.el('update-notice').hidden,false);
  assert.equal(h.el('update-notice').classList.contains('error'),false);
  assert.equal(h.calls.length,0);
});

test('snapshot failure disables the check button instead of dispatching',async()=>{
  const h=await updateHarness({state:'up_to_date',latest_version:'1.4.4',message:'当前已是最新正式版。'});
  assert.equal(h.el('check-updates').disabled,false);
  h.api.snapshot=async()=>{throw Error('snapshot unavailable');};await h.refresh();
  assert.equal(h.el('check-updates').disabled,true);
  clickUpdate(h,'check-updates');await settle();
  assert.equal(h.calls.length,0);
});

test('pending bridge actions keep the check button disabled even if a poll succeeds',async()=>{
  // 用「已是最新」而不是「准备安装」：后者本来就会禁止再点检查（代理正在准备安装），
  // 那样测不出「被进行中的操作挡住、操作结束后恢复」这件事。
  const h=await updateHarness({state:'up_to_date',latest_version:'1.4.4',message:'当前已是最新正式版。'});
  const dispatch=h.api.dispatch;let complete;
  h.api.dispatch=async(action,payload)=>{const result=await dispatch(action,payload);await new Promise(resolve=>complete=()=>resolve(result));return result;};
  h.stop.listeners.click();await settle();await h.refresh();
  assert.equal(h.el('check-updates').disabled,true);
  clickUpdate(h,'check-updates');
  assert.deepEqual(h.calls.map(call=>call.action),['network_stop']);
  complete();await settle();
  assert.equal(h.el('check-updates').disabled,false);
});

test('a ready update blocks another check while the agent prepares to install',async()=>{
  const h=await updateHarness(readyUpdate);
  assert.equal(h.el('update-status').textContent,'准备安装');
  assert.equal(h.el('check-updates').disabled,true);
  clickUpdate(h,'check-updates');await settle();
  assert.equal(h.calls.length,0);
});

test('an install that finished but could not relaunch is reported, not claimed as working',async()=>{
  const h=await updateHarness({...updateFixture({state:'error',current_version:'1.5.0',
    latest_version:'1.5.0',progress:100,
    message:'v1.5.0 已安装但程序没能启动，已停止重试并保留日志。'})});
  assert.equal(h.el('update-status').textContent,'更新未完成');
  assert.match(h.el('update-message').textContent,/没能启动/);
  assert.equal(h.el('update-notice').classList.contains('error'),true);
});

test('release messages and version strings are rendered literally, never as cloud HTML',async()=>{
  const message='<img src=x onerror=alert(1)> 下载失败，请重新检查。';
  const version='<b>1.5.0</b>';
  const h=await updateHarness({state:'error',message,latest_version:version});
  assert.equal(h.el('update-message').textContent,message);
  assert.equal(h.el('update-latest-version').textContent,version);
  assert.ok(h.el('update-notice-text').textContent.includes(message));
  assert.equal(h.el('update-notice-link').attributes.href,'#updates');
  assert.equal(h.calls.length,0);
});

/* What changed in this update ------------------------------------------------------------------ */
test('update entries are listed, and the card stays hidden while they are unknown',async()=>{
  const idle=await updateHarness();idle.navigate('updates');
  assert.equal(idle.el('update-changes').hidden,true,'No entries means no card');
  assert.equal(idle.el('update-changes-list').children.length,0);
  const h=await updateHarness({...readyUpdate,changes:changesFixture()});
  assert.equal(h.el('update-changes').hidden,false);
  assert.equal(h.el('update-changes-summary').textContent,'1.4.4 → v1.5.0 · 共 2 条');
  const listed=h.el('update-changes-list').children;
  assert.equal(listed.length,2);
  for(const item of listed)assert.equal(item.tagName,'LI');
  assert.equal(listed[0].textContent,'新增 · choose, name and manage the simulated position on a map · 2026-09-24');
  assert.equal(listed[1].textContent,'修复 · stop reporting a stale portal session as a healthy uplink · 2026-10-10');
  assert.equal(h.el('update-changes-more').hidden,true);
  // 有条目时说明为空，只留一个手工出口。
  assert.equal(h.el('update-changes-note').children[0].textContent,'');
  assert.equal(h.el('update-changes-note').children[1].textContent,'在 GitHub 上查看发布记录');
  // 拿不到说明时的出口：一句实话加一个手工链接。
  const bare=await updateHarness({...readyUpdate,changes:{entries:[],total:0,more:false,note:'没能自动获取这次更新的说明。'}});
  assert.equal(bare.el('update-changes').hidden,false);
  assert.equal(bare.el('update-changes-list').children.length,0);
  assert.equal(bare.el('update-changes-note').children[0].textContent,'没能自动获取这次更新的说明。 ');
  assert.equal(bare.el('update-changes-note').children[1].textContent,'在 GitHub 上查看发布记录');
  assert.equal(bare.el('update-changes-note').children[1].href,'https://github.com/yoouzic/youziauth/releases');
  assert.equal(bare.el('update-changes-note').children[1].target,'_blank');
  assert.equal(bare.el('update-changes-note').children[1].rel,'noopener noreferrer');
});

test('a long update list is capped and says how much it left out',async()=>{
  const h=await updateHarness({...readyUpdate,changes:{entries:manyEntries(20),total:20,more:true,note:''}});
  assert.equal(h.el('update-changes-list').children.length,12,'Only the first entries are listed inline');
  assert.match(h.el('update-changes-summary').textContent,/共 20 条/);
  assert.match(h.el('update-changes-more').textContent,/还有 8 条/);
  assert.equal(h.el('update-changes-more').hidden,false);
  // 后端快照封顶 60 条时 total 会比实际条数大，界面不能再报一个编出来的具体数字。
  const capped=await updateHarness({...readyUpdate,changes:{entries:manyEntries(60),total:200,more:true,note:''}});
  assert.equal(capped.el('update-changes-list').children.length,12);
  assert.match(capped.el('update-changes-summary').textContent,/更多改动/);
  assert.doesNotMatch(capped.el('update-changes-summary').textContent,/\d+ 条/);
  assert.match(capped.el('update-changes-more').textContent,/还有更多/);
  assert.doesNotMatch(capped.el('update-changes-more').textContent,/\d+ 条/);
  // 没顶到后端上限时仍然报准确数字：45 条里只带了 40 条，列 12 条，还剩 33 条。
  const exact=await updateHarness({...readyUpdate,changes:{entries:manyEntries(40),total:45,more:true,note:''}});
  assert.match(exact.el('update-changes-summary').textContent,/共 45 条/);
  assert.match(exact.el('update-changes-more').textContent,/还有 33 条/);
});

test('the change list never restates a version that is already installed',async()=>{
  for(const state of ['up_to_date','error','idle','checking']){
    const h=await updateHarness(updateFixture({state,changes:changesFixture()}));
    assert.equal(h.el('update-changes').hidden,true,'state '+state+' must not show change notes');
    assert.equal(h.el('update-changes-list').children.length,0);
  }
});

test('hand-written Chinese notes are shown as written, never retranslated',async()=>{
  const h=await updateHarness({...readyUpdate,changes:chineseChangesFixture()});
  const listed=h.el('update-changes-list').children.map(item=>item.textContent);
  assert.deepEqual(listed,[
    '校园网：代理开着时不再误报「能上网」',
    '重点 · 寝室打卡支持每个账号独立时段',
    '定位来源切换后立刻对自动打卡生效',
  ]);
  // 中文标签照原样用（不是 CHANGE_KINDS 里的英文键），也不该被追加日期。
  assert.doesNotMatch(listed.join('\n'),/校园网.*·\s*$/m);
  assert.equal(h.el('update-changes-summary').textContent,'1.4.4 → v1.5.0 · 共 3 条');
  assert.equal(h.el('update-changes-note').children[0].textContent,'这一版只改校园网和打卡。 ');
  assert.equal(h.el('update-changes-more').hidden,true);
});

test('commit subjects and notes from the cloud are rendered literally',async()=>{
  const subject='<img src=x onerror=alert(1)> fixed';
  const note='<script>alert(2)</script> 没能自动获取说明。';
  const h=await updateHarness({...readyUpdate,changes:{entries:[{subject,kind:'fix',date:'2026-10-10'}],total:1,more:false,note}});
  assert.equal(h.el('update-changes-list').children[0].textContent,'修复 · '+subject+' · 2026-10-10');
  assert.ok(h.el('update-changes-note').children[0].textContent.includes(note));
});

test('a snapshot without the change layer stays renderable',async()=>{
  const h=await updateHarness();await h.refresh();   // updateFixture ships no changes key at all
  h.navigate('updates');
  assert.equal(h.el('update-changes').hidden,true);
  assert.equal(h.el('update-changes-list').children.length,0);
  assert.equal(h.el('update-status').textContent,'等待检查');
  // 形状不对的云端字段只降级，不抛异常：轮询期间界面不能整块挂掉。
  for(const bad of [{entries:'nope',total:3,more:false,note:''},
                    {entries:[{subject:42},{subject:'   '},null],total:3,more:false,note:''}]){
    const broken=await updateHarness({...readyUpdate,changes:bad});
    assert.equal(broken.el('update-status').textContent,'准备安装','A bad change block must not break the update card');
    assert.equal(broken.el('update-changes').hidden,false);
    assert.equal(broken.el('update-changes').hidden,false);
    assert.equal(broken.el('update-changes-list').children.length,0);
  }
});

/* Simulation map picker ------------------------------------------------------------------- */
const mapZoom=h=>vm.runInContext('mapState?mapState.zoom:null',h.context);
const mapCentre=h=>vm.runInContext('mapState?mapState.center:null',h.context);
const mapPicked=h=>vm.runInContext('mapState?mapState.picked:null',h.context);
async function openMap(h){
  assert.equal(h.el('open-map-picker').hidden,false);
  h.el('open-map-picker').onclick();
  await settle();await settle();
}
function clickMap(h,x,y){h.el('map-canvas').listeners.click({offsetX:x,offsetY:y});}

test('the picker stays closed until it is opened, and only in simulation mode',async()=>{
  const windows=harness('windows');await settle();
  assert.equal(windows.el('open-map-picker').hidden,true);
  windows.el('open-map-picker').onclick();await settle();await settle();
  assert.equal(windows.el('map-dialog').open,false);
  assert.equal(windows.calls.filter(c=>c.action.startsWith('simulation')).length,0);

  const h=harness('simulation');await settle();
  assert.equal(h.el('open-map-picker').hidden,false);
  assert.equal(h.el('map-dialog').open,false);
  await openMap(h);
  assert.equal(h.el('map-dialog').open,true);
  assert.equal(h.el('open-map-picker').hidden,true);
  assert.match(h.el('map-summary').textContent,/示例宿舍/);
  assert.equal(h.el('map-save').disabled,true);
  assert.match(h.el('map-distance-badge').textContent,/^已保存 · 距基准点 \d+ 米（范围内）$/);
});

test('clicking the map picks a point and saving sends those coordinates',async()=>{
  const h=harness('simulation');await settle();
  await openMap(h);
  clickMap(h,320,180);
  assert.ok(Math.abs(mapPicked(h).latitude-29.823940)<0.00005);
  assert.ok(Math.abs(mapPicked(h).longitude-106.422470)<0.00005);
  assert.equal(h.el('map-save').disabled,false);
  assert.match(h.el('map-distance-badge').textContent,/^待保存 · 距基准点 0 米（范围内）$/);
  h.el('map-point-name').value='宿舍楼下';
  h.el('map-save').onclick();await settle();await settle();
  const save=h.calls.find(c=>c.action==='simulation_point_save');
  assert.ok(save,'saving must reach the bridge');
  assert.ok(Math.abs(save.payload.latitude-29.823940)<0.00005);
  assert.ok(Math.abs(save.payload.longitude-106.422470)<0.00005);
  assert.equal(mapPicked(h),null);
  assert.match(h.el('map-distance-badge').textContent,/^已保存 · /);
  assert.equal(h.el('map-points').options.length,2);
});

test('a pick outside the allowed radius is labelled before it is saved',async()=>{
  const h=harness('simulation');await settle();
  h.map.reference.radius_m=50;
  await openMap(h);
  clickMap(h,320,100);
  assert.match(h.el('map-distance-badge').textContent,/待保存 · 距基准点 \d+ 米（超出范围）$/);
  assert.equal(h.el('map-save').disabled,false);
});

test('saving is impossible without a pick',async()=>{
  const h=harness('simulation');await settle();
  await openMap(h);
  h.el('map-save').onclick();await settle();
  assert.equal(h.calls.length,0);
});

test('zoom is clamped and recentring returns to the school point',async()=>{
  const h=harness('simulation');await settle();
  await openMap(h);
  for(let i=0;i<12;i++)h.el('map-zoom-out').onclick();
  assert.equal(mapZoom(h),13);
  for(let i=0;i<12;i++)h.el('map-zoom-in').onclick();
  assert.equal(mapZoom(h),19);
  h.el('map-canvas').listeners.keydown({key:'ArrowUp',shiftKey:true,preventDefault(){}});
  assert.ok(Math.abs(mapCentre(h).latitude-29.823940)>0.001);
  h.el('map-recenter').onclick();
  assert.ok(Math.abs(mapCentre(h).latitude-29.823940)<1e-9);
  assert.ok(Math.abs(mapCentre(h).longitude-106.422470)<1e-9);
});

test('the basemap can be switched off without losing the picker',async()=>{
  const timers=fakeTimers(),images=fakeImages();
  const h=harness('simulation',{Image:images.FakeImage,setTimeout:timers.setTimeout,
                                clearTimeout:timers.clearTimeout});
  h.el('map-tiles').checked=true;await settle();
  await openMap(h);await settle();
  assert.equal(vm.runInContext('mapCredit(mapState)',h.context),'',
               'nothing is drawn yet, so there is no third-party data to credit');
  images.drain().forEach(image=>image.onload());await settle();
  assert.equal(vm.runInContext('mapCredit(mapState)',h.context),'© OpenStreetMap contributors',
               'the map data keeps its credit, drawn on the canvas instead of a paragraph');
  h.el('map-tiles').checked=false;h.el('map-tiles').listeners.change();
  assert.equal(vm.runInContext('mapCredit(mapState)',h.context),'',
               'no basemap means no third-party data to credit');
  assert.equal(h.el('map-dialog').open,true);
  // 冗长的底图说明段落已删除，只留画布上的一行署名。
  const markup=fs.readFileSync(path.join(__dirname,'../desktop_ui/index.html'),'utf8');
  assert.doesNotMatch(markup,/map-attribution/);
});

test('the picker still works before the school publishes a point',async()=>{
  const h=harness('simulation');await settle();
  h.map.reference=null;
  await openMap(h);
  assert.match(h.el('map-summary').textContent,/尚未查询今日任务/);
  assert.match(h.el('map-distance-badge').textContent,/^已保存 · 距基准点未知$/);
  clickMap(h,320,180);
  assert.equal(h.el('map-save').disabled,false);
  assert.match(h.el('map-distance-badge').textContent,/^待保存 · 距基准点未知$/);
  h.el('map-save').onclick();await settle();await settle();
  assert.ok(h.calls.some(c=>c.action==='simulation_point_save'));
});

/* Basemap providers, retries and failover -------------------------------------------------- */
const PROVIDERS=[
  {id:'osm',name:'OpenStreetMap',crs:'wgs84',max_zoom:19,
   url:'https://tile.openstreetmap.org/{z}/{x}/{y}.png',attribution:'© OpenStreetMap contributors'},
  {id:'amap',name:'高德地图',crs:'gcj02',max_zoom:18,
   url:'https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
   attribution:'© 高德地图'},
];
function bridgeTileUrls(){
  // Read the shipped provider table straight out of the bridge so the CSP cannot drift from it.
  const source=fs.readFileSync(path.join(__dirname,'../desktop_bridge.py'),'utf8');
  const start=source.indexOf('TILE_PROVIDERS = (');
  assert.ok(start>=0,'desktop_bridge.py must still declare TILE_PROVIDERS');
  const block=source.slice(start,source.indexOf('\n)\n',start));
  return [...block.matchAll(/'url':\s*((?:\s*'[^']*')+)/g)]
    .map(match=>[...match[1].matchAll(/'([^']*)'/g)].map(part=>part[1]).join(''));
}
function fakeTimers(){
  const queue=[];
  return {setTimeout(fn,ms){const job={fn,ms,cancelled:false};queue.push(job);return job;},
          clearTimeout(job){if(job)job.cancelled=true;},
          // run() fires everything queued; run(delay) only the jobs with that delay, so a test can
          // drive tile retries without also tripping the stall guard.
          run(...delays){
            for(const job of queue.splice(0,queue.length)){
              if(job.cancelled)continue;
              if(delays.length&&!delays.includes(job.ms)){queue.push(job);continue;}
              job.fn();
            }
          }};
}
function fakeImages(){
  const created=[];
  function FakeImage(){const image={src:'',onload:null,onerror:null,naturalWidth:256};created.push(image);return image;}
  return {FakeImage,created,drain(){return created.splice(0,created.length);}};
}

test('every basemap provider is allowed by the image policy and nothing else is',async()=>{
  const html=fs.readFileSync(path.join(__dirname,'../desktop_ui/index.html'),'utf8');
  const csp=html.match(/Content-Security-Policy" content="([^"]+)"/)[1];
  const policy=name=>csp.split(';').map(part=>part.trim()).find(part=>part.startsWith(name));
  const imagePolicy=policy('img-src'),connectPolicy=policy('connect-src');
  assert.ok(imagePolicy,'the page must state an img-src policy');
  assert.match(imagePolicy,/'self'/, 'same-origin images stay allowed');
  // The basemap is images only: no script, fetch or socket is opened to a third party.
  assert.equal(connectPolicy,"connect-src 'self'",'the picker must not open a third-party socket');

  // Compare the policy against the table the bridge actually ships, not against a fourth copy kept
  // here: renaming a tile host must fail this test rather than silently CSP-block the fallback.
  const shipped=bridgeTileUrls();
  assert.equal(shipped.length,2,'the bridge ships a preferred and a fallback provider');
  const origins=shipped.map(url=>new URL(url.replace(/\{[zxy]\}/g,'1')).origin);
  for(const origin of origins)
    assert.ok(imagePolicy.includes(origin),'img-src must name the shipped provider '+origin);
  const remote=[...new Set(csp.match(/https:\/\/[^\s;]+/g)||[])].sort();
  assert.deepEqual(remote,[...new Set(origins)].sort(),
                   'the picker adds exactly the shipped provider origins and nothing else');

  // ...and the UI walks the table in the order it was handed, without reordering it.
  const h=harness('simulation',{providers:PROVIDERS});await settle();
  await openMap(h);
  assert.deepEqual(vm.runInContext('mapProviders(mapState).map(provider => provider.url)',h.context),
                   PROVIDERS.map(provider=>provider.url));
  assert.equal(h.map.tile_url,shipped[0],
               'the fixture and the shipped table must agree, or this test proves nothing');
});

test('a tile that fails once is retried instead of poisoning the basemap',async()=>{
  const timers=fakeTimers(),images=fakeImages();
  const h=harness('simulation',{Image:images.FakeImage,setTimeout:timers.setTimeout,
                                clearTimeout:timers.clearTimeout});
  h.el('map-tiles').checked=true;await settle();await openMap(h);await settle();
  const first=images.drain();
  assert.ok(first.length>0,'the picker requests tiles');
  first.forEach(image=>image.onerror());
  const attempts=()=>vm.runInContext('Object.values(mapState.tiles).map(tile => tile.attempts)',h.context);
  assert.ok(attempts().every(count=>count===1),'one failure is not a write-off');
  assert.equal(vm.runInContext('mapBasemapFailed(mapState)',h.context),false);
  timers.run(1200,4000);await settle();
  assert.ok(images.created.length>0,'a failed tile is requested again');
  assert.ok(attempts().every(count=>count===2),'the retry is counted, not restarted');
  assert.equal(vm.runInContext('mapBasemapFailed(mapState)',h.context),false,
               'a pending retry is not reported as a failure');
});

test('a basemap that never loads says so and offers a retry',async()=>{
  const timers=fakeTimers(),images=fakeImages();
  const h=harness('simulation',{Image:images.FakeImage,setTimeout:timers.setTimeout,
                                clearTimeout:timers.clearTimeout});
  h.el('map-tiles').checked=true;await settle();await openMap(h);await settle();
  for(let round=0;round<4;round+=1){images.drain().forEach(image=>image.onerror());timers.run(1200,4000);await settle();}
  assert.equal(vm.runInContext('mapBasemapFailed(mapState)',h.context),true);
  assert.equal(h.el('map-basemap-note').hidden,false,'a dead basemap must not be silent');
  assert.match(h.el('map-basemap-note').textContent,/离线网格/);
  assert.equal(h.el('map-basemap-retry').hidden,false,'the user needs a way to try again');

  // Pressing retry starts over from the preferred provider with a clean slate.
  h.el('map-basemap-retry').onclick();await settle();
  assert.equal(h.el('map-basemap-retry').hidden,true);
  assert.equal(h.el('map-basemap-note').textContent,'正在加载在线底图…');
  assert.ok(images.drain().length>0,'retrying re-requests the tiles');
});

test('a basemap that hangs instead of failing still hands over to the fallback',async()=>{
  // The reported bug: the tiles neither loaded nor errored, so an image handler never fired and
  // the picker sat on a bare grid for good. Only a stall guard can see that.
  const timers=fakeTimers(),images=fakeImages();
  const h=harness('simulation',{providers:PROVIDERS,Image:images.FakeImage,
                                setTimeout:timers.setTimeout,clearTimeout:timers.clearTimeout});
  h.el('map-tiles').checked=true;await settle();await openMap(h);await settle();
  assert.equal(vm.runInContext('mapState.provider_index',h.context),0);
  assert.ok(images.drain().length>0,'the preferred provider is asked first');
  assert.equal(vm.runInContext('mapBasemapFailed(mapState)',h.context),false,
               'a request still in flight is not a failure');
  // Filter by the guard's own delay: changing MAP_TILE_STALL must break this test, because the
  // window is a deliberate user-facing latency budget, not an implementation detail.
  timers.run(8000);await settle();
  assert.equal(vm.runInContext('mapState.provider_index',h.context),1,'the stall hands the frame over');
  assert.match(images.created[0].src,/autonavi/,'the fallback is asked for it');
});

test('a basemap that hangs with no fallback left is reported, not left blank',async()=>{
  const timers=fakeTimers(),images=fakeImages();
  const h=harness('simulation',{Image:images.FakeImage,setTimeout:timers.setTimeout,
                                clearTimeout:timers.clearTimeout});
  h.el('map-tiles').checked=true;await settle();await openMap(h);await settle();
  images.drain();
  timers.run(8000);await settle();          // the stall window, pinned by its delay
  assert.equal(vm.runInContext('mapBasemapFailed(mapState)',h.context),true);
  assert.match(h.el('map-basemap-note').textContent,/离线网格/);
  assert.equal(h.el('map-basemap-retry').hidden,false);
});

test('a dead preferred basemap fails over to the fallback provider on its own',async()=>{
  const timers=fakeTimers(),images=fakeImages();
  const h=harness('simulation',{providers:PROVIDERS,Image:images.FakeImage,
                                setTimeout:timers.setTimeout,clearTimeout:timers.clearTimeout});
  h.el('map-tiles').checked=true;await settle();await openMap(h);await settle();
  assert.equal(vm.runInContext('mapState.provider_index',h.context),0);
  assert.match(images.created[0].src,/openstreetmap/,'the preferred provider goes first');

  // Three answers each: two retries, then the third failure is terminal and the fallback takes over.
  for(let round=0;round<3;round+=1){images.drain().forEach(image=>image.onerror());timers.run(1200,4000);await settle();}
  await settle();
  assert.equal(vm.runInContext('mapState.provider_index',h.context),1,'it moves to the fallback');
  const fallback=images.created.find(image=>/autonavi/.test(image.src));
  assert.ok(fallback,'the fallback provider is asked for the same frame');
  fallback.onload();await settle();
  assert.equal(vm.runInContext('mapCredit(mapState)',h.context),'© 高德地图',
               'the credit follows the provider that actually drew');
  assert.match(h.el('map-basemap-note').textContent,/备用底图：高德地图/);
  assert.equal(h.el('map-basemap-note').classList.contains('warn'),false);
});

test('the fallback provider caps the zoom at the level it actually serves',async()=>{
  const h=harness('simulation',{providers:PROVIDERS});
  h.el('map-tiles').checked=true;await settle();await openMap(h);await settle();
  assert.equal(vm.runInContext('mapMaxZoom(mapState)',h.context),19);
  vm.runInContext('mapState.provider_index=1;mapState.zoom=19;renderMap()',h.context);
  assert.equal(mapZoom(h),18,'高德 serves nothing past 18, so the frame stops there');
  vm.runInContext('zoomMap(5)',h.context);
  assert.equal(mapZoom(h),18);
});

test('a GCJ02 basemap shifts the tiles, never the coordinates',async()=>{
  const h=harness('simulation',{providers:PROVIDERS});
  h.el('map-tiles').checked=true;await settle();await openMap(h);await settle();
  // The browser and dorm_location.wgs84_to_gcj02 must agree on the offset, or a picked point
  // would land hundreds of metres from the campus drawn underneath it.
  const gcj=vm.runInContext('mapToGcj02(29.823693,106.422310)',h.context);
  assert.ok(Math.abs(gcj.latitude-29.821209490450343)<1e-9,'latitude offset matches the bridge');
  assert.ok(Math.abs(gcj.longitude-106.42634346477006)<1e-9,'longitude offset matches the bridge');
  const outside=vm.runInContext('mapToGcj02(51.5,-0.1)',h.context);
  assert.equal(outside.latitude,51.5,'outside China there is no offset');
  assert.equal(outside.longitude,-0.1);
  // A WGS84 provider draws on the plain WGS84 grid.
  const plain=vm.runInContext('mapTileCentre(mapState)',h.context);
  assert.equal(plain.latitude,mapCentre(h).latitude);
  assert.equal(plain.longitude,mapCentre(h).longitude);
  // The GCJ02 provider draws its own offset grid...
  vm.runInContext('mapState.provider_index=1',h.context);
  const offset=vm.runInContext('mapTileCentre(mapState)',h.context);
  assert.ok(Math.abs(offset.latitude-mapCentre(h).latitude)>0.002,'the tile grid is offset');
  // ...while the frame the user clicks in stays WGS84, so a centre click is the centre point.
  clickMap(h,320,180);
  assert.ok(Math.abs(mapPicked(h).latitude-mapCentre(h).latitude)<1e-6);
  assert.ok(Math.abs(mapPicked(h).longitude-mapCentre(h).longitude)<1e-6);
});

test('the GCJ02 tile offset cancels against WGS84 markers to sub-pixel accuracy',async()=>{
  // The whole alignment argument: a marker drawn from WGS84 must land where the GCJ02 tile grid
  // puts that same place. The offset field is only locally constant, so this is an approximation -
  // measure it instead of trusting it, and fail if it ever stops being sub-pixel.
  const h=harness('simulation',{providers:PROVIDERS});
  h.el('map-tiles').checked=true;await settle();await openMap(h);await settle();
  vm.runInContext('mapState.provider_index=1',h.context);          // the GCJ02 provider
  const measured=vm.runInContext(`(() => {
    const size=mapSize($('map-canvas'));
    const grid={...mapState,center:mapTileCentre(mapState)};
    const offset=Math.abs(grid.center.latitude-mapState.center.latitude);
    let worst=0;
    for(const dx of [-size.width/2,0,size.width/2])for(const dy of [-size.height/2,0,size.height/2]){
      const point=mapPointAt(mapState,size,size.width/2+dx,size.height/2+dy);
      const marker=mapScreen(mapState,point.latitude,point.longitude,size);
      const gcj=mapToGcj02(point.latitude,point.longitude);
      const tile=mapScreen(grid,gcj.latitude,gcj.longitude,size);
      worst=Math.max(worst,Math.hypot(marker.x-tile.x,marker.y-tile.y));
    }
    return {worst,offset,zoom:mapState.zoom};
  })()`,h.context);
  assert.ok(measured.offset>0.002,'the tile grid must actually be offset, or this proves nothing');
  assert.ok(measured.worst<2,
            `marker and basemap disagree by ${measured.worst.toFixed(2)} px at zoom ${measured.zoom}`);
});

test('a reload while tiles are in flight must not strand them',async()=>{
  // Saving, switching, renaming or deleting a point reloads the model with the dialog still open.
  // applyMapModel replaces the state object but keeps the tile map, so anything that identified a
  // live tile by comparing the state object silently lost its repaint, its retry and its failover.
  const timers=fakeTimers(),images=fakeImages();
  const h=harness('simulation',{Image:images.FakeImage,setTimeout:timers.setTimeout,
                                clearTimeout:timers.clearTimeout});
  h.el('map-tiles').checked=true;await settle();await openMap(h);await settle();
  const inFlight=images.drain();
  assert.ok(inFlight.length>0,'tiles were requested');
  inFlight[0].onload();await settle();
  await vm.runInContext('reloadMap()',h.context);await settle();
  assert.equal(vm.runInContext('mapTileCounts(mapState).ready',h.context),1,
               'the tile that arrived before the reload is still on the map');

  const attempts=()=>vm.runInContext('Object.values(mapState.tiles).map(tile => tile.attempts)',h.context);
  inFlight.slice(1).forEach(image=>image.onerror());
  assert.ok(attempts().every(count=>count===1));
  timers.run(1200,4000);await settle();
  assert.ok(images.created.length>0,'a reload must not strand in-flight tiles without a retry');
  assert.equal(attempts().filter(count=>count===2).length,inFlight.length-1,
               'every stranded tile is retried, not just one');
});

test('one tile arriving must not disarm the deadline on the others',async()=>{
  // The single global stall guard stands down as soon as anything is drawn. Without a per-tile
  // deadline the rest of the frame can hang for good with no retry and an empty status line.
  const timers=fakeTimers(),images=fakeImages();
  const h=harness('simulation',{Image:images.FakeImage,setTimeout:timers.setTimeout,
                                clearTimeout:timers.clearTimeout});
  h.el('map-tiles').checked=true;await settle();await openMap(h);await settle();
  const first=images.drain();
  first[0].onload();await settle();
  timers.run(8000);await settle();               // the others never load and never error
  assert.equal(images.created.length,0,'the deadline itself issues no request');
  timers.run(1200,4000);await settle();          // ...it schedules the same retry an error would
  assert.ok(images.created.length>0,'a hung tile must be retried even when the frame is not empty');
  const attempts=vm.runInContext('Object.values(mapState.tiles).map(tile => tile.attempts)',h.context);
  assert.equal(attempts.filter(count=>count===2).length,first.length-1,
               'every hung tile is retried, exactly once so far');
});

test('a late tile clears the offline message it contradicts',async()=>{
  const timers=fakeTimers(),images=fakeImages();
  const h=harness('simulation',{Image:images.FakeImage,setTimeout:timers.setTimeout,
                                clearTimeout:timers.clearTimeout});
  h.el('map-tiles').checked=true;await settle();await openMap(h);await settle();
  const first=images.drain();
  timers.run(8000);await settle();               // nothing drawn and no fallback left: it reports
  assert.equal(h.el('map-basemap-retry').hidden,false);
  assert.match(h.el('map-basemap-note').textContent,/离线网格/);
  first[0].onload();await settle();
  assert.equal(vm.runInContext('mapTileCounts(mapState).ready',h.context),1);
  assert.equal(h.el('map-basemap-note').hidden,true,
               'the grid is gone, so the warning about the grid must go too');
  assert.equal(h.el('map-basemap-retry').hidden,true);
});

test('closing the picker clears the basemap warning and the retry control',async()=>{
  const timers=fakeTimers(),images=fakeImages();
  const h=harness('simulation',{Image:images.FakeImage,setTimeout:timers.setTimeout,
                                clearTimeout:timers.clearTimeout});
  h.el('map-tiles').checked=true;await settle();await openMap(h);await settle();
  images.drain();
  timers.run(8000);await settle();
  assert.equal(h.el('map-basemap-note').hidden,false);
  h.el('map-close').onclick();await settle();
  assert.equal(h.el('map-basemap-note').hidden,true,'a closed picker shows no status line');
  assert.equal(h.el('map-basemap-note').textContent,'');
  assert.equal(h.el('map-basemap-retry').hidden,true);

  // And reopening after a failure starts clean rather than inheriting the warning.
  await openMap(h);await settle();
  assert.equal(h.el('map-basemap-note').textContent,'正在加载在线底图…');
  assert.equal(h.el('map-basemap-retry').hidden,true);
});

test('a click is read through the padding box, not the border box',async()=>{
  // offsetX is measured from the padding edge while getBoundingClientRect returns the border box.
  // Scaling by the border box skews every pick further out the closer it is to the right and bottom
  // edges - about a metre at zoom 17, and a canvas pixel is a metre. Measured on the real window:
  // the frame is 1 px, the canvas is 720x400 displayed at 713x396 of padding box.
  const width=720,height=400;
  const rect={left:201,top:280.390625,width:715,height:398.109375};   // border box, 1 px frame
  const padding={width:rect.width-2,height:rect.height-2};
  const h=harness('simulation',{canvasRect:rect,
    getComputedStyle:()=>({borderLeftWidth:'1px',borderRightWidth:'1px',
                           borderTopWidth:'1px',borderBottomWidth:'1px'})});
  h.el('map-canvas').width=width;h.el('map-canvas').height=height;
  await settle();await openMap(h);await settle();
  for(const target of [{x:100,y:120},{x:359,y:200},{x:610,y:330}]){
    // A real pointer reports whole CSS pixels; aim at the canvas pixel we want and click.
    const offsetX=target.x*padding.width/width,offsetY=target.y*padding.height/height;
    clickMap(h,offsetX,offsetY);
    // The pick is stored rounded to six decimals (about 0.11 m), which is far finer than the
    // ~1 px skew this test exists to catch.
    const expected=vm.runInContext(
      `(() => {const point=mapPointAt(mapState,{width:${width},height:${height}},${target.x},${target.y});
               return {latitude:mapRound(point.latitude),longitude:mapRound(point.longitude)};})()`,
      h.context);
    const picked=mapPicked(h);
    assert.equal(picked.latitude,expected.latitude,
                 `click at canvas ${JSON.stringify(target)} landed off the padding-box pixel`);
    assert.equal(picked.longitude,expected.longitude,
                 `click at canvas ${JSON.stringify(target)} landed off the padding-box pixel`);
  }
});

test('dragging pans the map and never drops a marker',async()=>{
  const h=harness('simulation');await settle();
  await openMap(h);
  const canvas=h.el('map-canvas'),before=mapCentre(h);
  canvas.listeners.pointerdown({offsetX:360,offsetY:200,pointerId:1});
  canvas.listeners.pointermove({offsetX:300,offsetY:200,pointerId:1});
  canvas.listeners.pointerup({offsetX:300,offsetY:200,pointerId:1});
  const after=mapCentre(h);
  assert.ok(after.longitude>before.longitude,'dragging left moves the view east');
  assert.ok(after.latitude-before.latitude<1e-9,'a horizontal drag does not tilt the view');
  assert.equal(mapPicked(h),null,'a drag is not a pick');
  canvas.listeners.click({offsetX:300,offsetY:200});
  assert.equal(mapPicked(h),null,'the click that ends a drag is swallowed');
  canvas.listeners.click({offsetX:320,offsetY:180});
  assert.ok(mapPicked(h),'a plain click still picks');
});

test('the wheel zooms and stays inside the allowed range',async()=>{
  const h=harness('simulation');await settle();
  await openMap(h);
  const canvas=h.el('map-canvas'),start=mapZoom(h);
  canvas.listeners.wheel({deltaY:-120,preventDefault(){}});
  assert.equal(mapZoom(h),start+1);
  for(let step=0;step<12;step++)canvas.listeners.wheel({deltaY:120,preventDefault(){}});
  assert.equal(mapZoom(h),13,'zooming out stops at the minimum');
  for(let step=0;step<12;step++)canvas.listeners.wheel({deltaY:-120,preventDefault(){}});
  assert.equal(mapZoom(h),19,'zooming in stops at the maximum');
});

test('saved points can be listed, switched, renamed and deleted',async()=>{
  const h=harness('simulation');await settle();
  await openMap(h);
  const select=h.el('map-points');
  assert.deepEqual(select.options.map(option=>option.value),['p1']);
  assert.match(select.options[0].textContent,/^本机采样样本 · \d+ 米$/);
  assert.equal(select.value,'p1');
  assert.match(h.el('map-point-state').textContent,/当前生效：本机采样样本/);
  assert.equal(h.el('map-point-name').value,'本机采样样本');

  clickMap(h,360,200);
  h.el('map-point-name').value='图书馆北门';
  h.el('map-save').onclick();await settle();await settle();
  const save=h.calls.find(call=>call.action==='simulation_point_save');
  assert.equal(save.payload.name,'图书馆北门','the name travels with the pick');
  assert.equal(h.el('map-points').options.length,2);
  assert.match(h.el('map-point-state').textContent,/图书馆北门/);

  h.el('map-points').value='p1';
  h.el('map-points').listeners.change();await settle();await settle();
  assert.ok(h.calls.some(call=>call.action==='simulation_point_select'&&call.payload.id==='p1'),
            'choosing from the dropdown switches the active point');
  assert.equal(h.el('map-points').value,'p1');
  assert.equal(h.el('map-point-name').value,'本机采样样本','the box follows the switched point');

  h.el('map-point-name').value='原始采样';
  h.el('map-point-rename').onclick();await settle();await settle();
  const rename=h.calls.find(call=>call.action==='simulation_point_rename');
  assert.equal(rename.payload.id,'p1');
  assert.equal(rename.payload.name,'原始采样');
  assert.match(h.el('map-point-state').textContent,/原始采样/);

  h.el('map-point-delete').onclick();
  assert.match(h.el('confirm-title').textContent,/删除选点「原始采样」/);
  h.el('confirm-ok').onclick();await settle();await settle();
  assert.ok(h.calls.some(call=>call.action==='simulation_point_delete'&&call.payload.id==='p1'));
  // Deleting the active point switches to the remaining one instead of losing the position.
  assert.deepEqual(h.el('map-points').options.map(option=>option.value),['p2']);
  assert.equal(h.el('map-points').value,'p2');
  assert.match(h.el('map-point-state').textContent,/当前生效：图书馆北门/);
});

test('map markers carry the point name instead of a generic label',async()=>{
  const h=harness('simulation');await settle();
  await openMap(h);
  assert.equal(vm.runInContext('mapSavedLabel(mapState)',h.context),'本机采样样本');
  clickMap(h,320,180);
  assert.equal(vm.runInContext('mapPendingLabel(mapState)',h.context),'待保存：本机采样样本',
               'a pick keeps the active name, so the marker says which point it would move');
  h.el('map-point-name').value='杏园三舍';
  assert.equal(vm.runInContext('mapPendingLabel(mapState)',h.context),'待保存：杏园三舍',
               'the pending marker shows the name being typed');
  h.el('map-save').onclick();await settle();await settle();
  assert.equal(vm.runInContext('mapSavedLabel(mapState)',h.context),'杏园三舍');
});

test('the name box survives background redraws while it is being edited',async()=>{
  // Tile loads redraw the map constantly; the box used to be rewritten from the active point on
  // every redraw, which silently replaced whatever the user was typing.
  const h=harness('simulation');await settle();
  await openMap(h);
  h.el('map-point-name').value='宿舍东门';
  vm.runInContext('renderMap()',h.context);      // what a tile arriving mid-typing does
  vm.runInContext('renderMap()',h.context);
  assert.equal(h.el('map-point-name').value,'宿舍东门','typing must not be wiped');
  h.el('map-point-rename').onclick();await settle();await settle();
  const rename=h.calls.find(call=>call.action==='simulation_point_rename');
  assert.equal(rename.payload.name,'宿舍东门','the typed name is what gets renamed');
  assert.match(h.el('map-point-state').textContent,/当前生效：宿舍东门/);
  assert.equal(h.el('map-point-name').value,'宿舍东门');
});

test('a sample that is not a saved point is offered as itself',async()=>{
  const h=harness('simulation');await settle();
  h.map.points=[];h.map.active_id='';
  await openMap(h);
  assert.equal(h.el('map-points').options.length,1);
  assert.match(h.el('map-points').options[0].textContent,/当前样本（未命名）/);
  assert.match(h.el('map-point-state').textContent,/当前生效：未命名的样本/);
  assert.equal(h.el('map-point-rename').disabled,true);
  assert.equal(h.el('map-point-delete').disabled,true);
});

test('closing the map or leaving simulation mode hides the picker',async()=>{
  const h=harness('simulation');await settle();
  await openMap(h);
  h.el('map-close').onclick();
  assert.equal(h.el('map-dialog').open,false);
  assert.equal(h.el('open-map-picker').hidden,false);
  await openMap(h);
  h.data.dorm.settings.location_source='windows';
  await h.refresh();
  assert.equal(h.el('map-dialog').open,false);
  assert.equal(h.el('open-map-picker').hidden,true);
});

/* 打卡账号切换器 -------------------------------------------------------------------------
   所有已启用自动打卡的账号都在各自时段内打卡，state.dorm 仍然只描述活动账号，所以切换后整页
   跟着换；账号栏要能从每个账号自己的字段看出它今天的状态。 */
const ACCOUNT_CONTROLS=['dorm-account','account-name','account-add','account-rename','account-delete'];
function pickAccount(h,id){h.el('dorm-account').value=id;h.el('dorm-account').listeners.change();}

test('the account bar lists every account with its student id and selects the active one',async()=>{
  const h=harness();await settle();
  const select=h.el('dorm-account');
  assert.equal(h.el('account-bar').hidden,false);
  assert.deepEqual(select.options.map(option=>option.value),['a1','a2']);
  assert.deepEqual(select.options.map(option=>option.textContent),
                   ['账号 1 · 2023123456 · 等待时段','账号 2 · 未开启自动打卡']);
  assert.equal(select.value,'a1');
  assert.equal(select.disabled,false);
  assert.equal(h.el('account-name').value,'账号 1','名字框回填的是活动账号');
  assert.equal(h.el('account-state').textContent,'自动打卡：1 个账号已开启 · 0 个今日已完成');
  for(const id of ACCOUNT_CONTROLS)assert.equal(h.el(id).disabled,false);
});

test('a missing profile is marked and a refused switch leaves the active account selected',async()=>{
  const h=harness('windows',{accounts:{items:[
    {id:'a1',name:'账号 1',active:true,has_idm_credentials:true,idm_username:'2023123456',missing:false},
    {id:'a2',name:'账号 2',active:false,has_idm_credentials:false,idm_username:'',missing:true}]}});
  await settle();
  assert.match(h.el('dorm-account').options[1].textContent,/档案缺失/);
  pickAccount(h,'a2');await settle();
  assert.equal(h.el('confirm-dialog').open,false,'没有未保存的改动就不必再确认一次');
  assert.deepEqual(h.calls.map(call=>call.action),['account_switch']);
  assert.equal(h.calls[0].payload.id,'a2');
  assert.equal(h.el('dorm-account').value,'a1','后端拒绝后下拉框必须回到活动账号');
  assert.match(h.el('toast').textContent,/档案目录不存在/);
});

test('choosing the account that is already active does nothing at all',async()=>{
  const h=harness();await settle();
  h.el('auto-interval').value='900';h.el('dorm-form').listeners.input();
  pickAccount(h,'a1');
  assert.equal(h.el('confirm-dialog').open,false);
  assert.equal(h.calls.length,0);
  assert.equal(h.el('dorm-save-state').textContent,'有未保存的修改','未保存的改动不该被这个空操作丢掉');
});

test('switching with unsaved dorm edits asks first, then fills the form from the new account',async()=>{
  const h=harness();await settle();
  h.el('auto-interval').value='900';h.el('dorm-form').listeners.input();
  pickAccount(h,'a2');
  assert.equal(h.el('confirm-dialog').open,true);
  assert.equal(h.el('confirm-title').textContent,'切换账号？');
  assert.match(h.el('confirm-text').textContent,/未保存.*放弃/);
  assert.equal(h.calls.length,0,'确认之前不能切换');
  assert.equal(h.el('dorm-account').value,'a1','还没切换成功，下拉框先回到活动账号');
  h.el('confirm-ok').onclick();await settle();await settle();
  assert.deepEqual(h.calls.map(call=>call.action),['account_switch']);
  assert.equal(h.calls[0].payload.id,'a2');
  assert.equal(h.el('dorm-account').value,'a2');
  assert.equal(String(h.el('auto-interval').value),'600','新账号的设置要灌进表单');
  assert.equal(h.el('auto-enabled').checked,true);
  assert.equal(h.el('location-source').value,'windows');
  assert.equal(h.el('dorm-save-state').textContent,'设置已同步');
});

test('switching accounts closes the map picker and restores the new account location source',async()=>{
  const h=harness('simulation');await settle();
  await openMap(h);
  assert.equal(h.el('map-dialog').open,true);
  pickSource(h,'windows');
  pickAccount(h,'a2');
  assert.equal(h.el('confirm-dialog').open,true);
  h.el('confirm-ok').onclick();await settle();await settle();
  assert.equal(h.el('map-dialog').open,false,'旧账号的选点面板不能留给新账号');
  assert.equal(h.el('location-source').value,'simulation','新账号保存的定位来源灌回表单');
  assert.equal(h.el('dorm-save-state').textContent,'设置已同步');
});

test('a switch confirmed after the state moved on is refused instead of switching to a stale account',async()=>{
  const h=harness();await settle();
  h.el('auto-interval').value='900';h.el('dorm-form').listeners.input();
  pickAccount(h,'a2');
  assert.equal(h.el('confirm-dialog').open,true);
  h.stop.listeners.click();await settle();          // 别的操作先落地，确认框里的选择已经过期
  h.el('confirm-ok').onclick();await settle();await settle();
  assert.deepEqual(h.calls.map(call=>call.action),['network_stop']);
  assert.equal(h.el('dorm-account').value,'a1');
  assert.match(h.el('toast').textContent,/重新选择/);
});

for(const name of ['','实验室二号']){
  test(`a new account takes the typed name "${name}" and empties the box for the next one`,async()=>{
    const h=harness();await settle();
    h.el('account-name').value=name;
    h.el('account-add').onclick();await settle();await settle();
    assert.deepEqual(h.calls.map(call=>call.action),['account_add']);
    assert.equal(h.calls[0].payload.name,name,'输入框里的名字原样交给后端，留空由后端自动命名');
    assert.equal(h.el('account-name').value,'');
    assert.deepEqual(h.el('dorm-account').options.map(option=>option.value),['a1','a2','a3']);
    assert.equal(h.el('dorm-account').value,'a3','新建的账号成为活动账号');
    assert.equal(String(h.el('auto-interval').value),'300','新账号的默认设置灌进表单');
    assert.equal(h.el('dorm-save-state').textContent,'设置已同步');
  });
}

test('renaming an account needs a name and then refills the box from the stored one',async()=>{
  const h=harness();await settle();
  h.el('account-name').value='';
  h.el('account-rename').onclick();await settle();
  assert.equal(h.calls.length,0,'空名字不发请求');
  assert.equal(h.el('toast').textContent,'请填写账号名称，最多 24 个字。');
  h.el('account-name').value='  杏园三舍  ';
  h.el('account-rename').onclick();await settle();await settle();
  const rename=h.calls.find(call=>call.action==='account_rename');
  assert.equal(rename.payload.id,'a1','重命名的是当前活动账号');
  assert.equal(rename.payload.name,'  杏园三舍  ','名字原样交给后端清洗');
  assert.equal(h.el('account-name').value,'杏园三舍','回填的是后端清洗后的名字');
  assert.equal(h.el('dorm-account').options[0].textContent,'杏园三舍 · 2023123456 · 等待时段');
  assert.equal(h.el('dorm-account').options[0].value,'a1');
});

test('deleting an account names it, asks first and confirms it to the backend',async()=>{
  const h=harness();await settle();
  h.el('account-delete').onclick();
  assert.equal(h.el('confirm-title').textContent,'删除账号「账号 1」？');
  assert.match(h.el('confirm-text').textContent,/已保存的打卡点/);
  assert.match(h.el('confirm-text').textContent,/无法撤销/);
  assert.equal(h.calls.length,0,'确认之前不能删除');
  h.el('confirm-cancel').onclick();await settle();
  assert.equal(h.calls.length,0);
  assert.deepEqual(h.el('dorm-account').options.map(option=>option.value),['a1','a2'],'取消后账号还在');
  h.el('account-delete').onclick();h.el('confirm-ok').onclick();await settle();await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)),
                   [{action:'account_delete',payload:{id:'a1',confirmed:true}}]);
  assert.deepEqual(h.el('dorm-account').options.map(option=>option.value),['a2']);
  assert.equal(h.el('dorm-account').value,'a2','剩下的账号成为活动账号');
  assert.equal(h.el('account-name').value,'账号 2');
});

test('a snapshot without an account layer hides the bar and leaves the rest of the page working',async()=>{
  const h=harness('windows',{accounts:false});await settle();
  assert.equal(h.el('account-bar').hidden,true,'老后端没有账号层，整条栏不出现');
  assert.equal(h.el('dorm-account').options.length,0);
  h.data.dorm.state='ready';await h.refresh();
  h.el('authorize-location').listeners.click();await settle();
  await saveSchedule(h);
  assert.deepEqual(h.calls.map(call=>call.action),['location_authorize','dorm_save']);
  assert.equal(h.el('submit-dorm').disabled,false,'打卡按钮照常可用');
});

test('an unreadable account list shows its own message and disables every account control',async()=>{
  const h=harness('windows',{accounts:{error:'账号清单读取失败：accounts.json 已损坏。',items:[]}});await settle();
  assert.equal(h.el('account-bar').hidden,false,'读不出来也要说明原因，不能静默消失');
  assert.equal(h.el('account-state').textContent,'账号清单读取失败：accounts.json 已损坏。');
  for(const id of ACCOUNT_CONTROLS)assert.equal(h.el(id).disabled,true,id+' 在清单读不出来时必须禁用');
  h.el('account-add').onclick();h.el('account-rename').onclick();h.el('account-delete').onclick();await settle();
  assert.equal(h.calls.length,0,'清单读不出来时后端会拒绝每一个账号操作');
});

test('the account limit disables adding and says how many are allowed',async()=>{
  const h=harness('windows',{accounts:{max:2}});await settle();
  assert.equal(h.el('account-add').disabled,true);
  assert.equal(h.el('account-state').textContent,'最多 2 个账号');
  h.data.accounts.items.pop();await h.refresh();
  assert.equal(h.el('account-add').disabled,false);
  assert.equal(h.el('account-state').textContent,'');
});

test('account controls are disabled while an account action is in flight',async()=>{
  const h=harness();await settle();
  for(const id of ACCOUNT_CONTROLS)assert.equal(h.el(id).disabled,false);
  h.api.dispatch=(action,payload)=>new Promise(resolve=>{
    h.calls.push({action,payload});
    h.complete=()=>resolve({ok:true,message:'操作完成'});
  });
  h.el('account-add').onclick();await settle();
  for(const id of ACCOUNT_CONTROLS)assert.equal(h.el(id).disabled,true,'请求还没回来，不能再发一次账号操作');
  h.complete();await settle();await settle();
  assert.equal(h.el('account-add').disabled,false);
});

/* 每个账号各自的今日状态 ----------------------------------------------------------------- */
function accountItem(overrides={}){
  return {id:'a1',name:'账号 1',active:false,has_idm_credentials:false,idm_username:'',missing:false,
          enabled:false,state:'idle',busy:false,signed_today:false,...overrides};
}

test('the account picker spells out each account with one status suffix',async()=>{
  const h=harness('windows',{accounts:{max:11,items:[
    accountItem({id:'a1',name:'账号 1',active:true,idm_username:'2023123456',enabled:true,state:'signed',signed_today:true}),
    accountItem({id:'a2',name:'账号 2',enabled:true,state:'waiting'}),
    accountItem({id:'a3',name:'账号 3'}),
    accountItem({id:'a4',name:'账号 4',missing:true,enabled:true,state:'waiting'}),
    accountItem({id:'a5',name:'账号 5',busy:true,enabled:true,state:'busy'}),
    accountItem({id:'a6',name:'账号 6',enabled:true,state:'login_required'}),
    accountItem({id:'a7',name:'账号 7',enabled:true,state:'location_required'}),
    accountItem({id:'a8',name:'账号 8',enabled:true,state:'network_error'}),
    accountItem({id:'a9',name:'账号 9',enabled:true,state:'error'}),
    accountItem({id:'a10',name:'账号 10',enabled:true,state:'ready'}),
    accountItem({id:'a11',name:'账号 11',enabled:true,state:'no_task'})]}});
  await settle();
  assert.deepEqual(h.el('dorm-account').options.map(option=>option.textContent),[
    '账号 1 · 2023123456 · 今日已完成',
    '账号 2 · 等待时段',
    '账号 3 · 未开启自动打卡',
    '账号 4 · 档案缺失',
    '账号 5 · 处理中',
    '账号 6 · 需要重新登录',
    '账号 7 · 需要检查定位',
    '账号 8 · 需要检查',
    '账号 9 · 需要检查',
    '账号 10 · 今日待打卡',
    '账号 11 · 今天暂无任务'],'后缀按优先级只取第一个命中的');
});

test('the account bar summarises how many accounts auto check in and how many are done',async()=>{
  const h=harness('windows',{accounts:{items:[
    accountItem({id:'a1',name:'账号 1',active:true,enabled:true,signed_today:true}),
    accountItem({id:'a2',name:'账号 2',enabled:true}),
    accountItem({id:'a3',name:'账号 3',enabled:true}),
    accountItem({id:'a4',name:'账号 4',signed_today:true})]}});
  await settle();
  assert.equal(h.el('account-state').textContent,'自动打卡：3 个账号已开启 · 2 个今日已完成',
               '已完成的账号不必开启自动打卡也算数');
  h.data.accounts.items[1].enabled=false;h.data.accounts.items[2].enabled=false;await h.refresh();
  assert.equal(h.el('account-state').textContent,'自动打卡：1 个账号已开启 · 2 个今日已完成');
});

test('the summary stays empty without an enabled account and with a single account',async()=>{
  const h=harness('windows',{accounts:{items:[
    accountItem({id:'a1',name:'账号 1',active:true,signed_today:true}),
    accountItem({id:'a2',name:'账号 2'})]}});
  await settle();
  assert.equal(h.el('account-state').textContent,'','没有账号开着自动打卡就不必汇总');
  h.data.accounts.items=[accountItem({id:'a1',name:'账号 1',active:true,enabled:true,signed_today:true})];
  await h.refresh();
  assert.equal(h.el('account-state').textContent,'','单账号的自动打卡状态由下面的设置卡片说明');
});

test('a running check-in no longer disables the account controls',async()=>{
  const h=harness();await settle();
  h.data.dorm.busy=true;await h.refresh();
  assert.equal(h.el('dorm-account').disabled,false,'本阶段换账号只是界面行为，不用等打卡结束');
  h.data.accounts.busy=true;await h.refresh();
  for(const id of ACCOUNT_CONTROLS)assert.equal(h.el(id).disabled,false,'别的账号在打卡也不拦着账号栏：'+id);
  assert.equal(h.el('logout-dorm').disabled,true,'state.dorm.busy 的既有逻辑保持原样');
  assert.equal(h.el('cancel-dorm').disabled,false);
});

test('cancelling reaches every account, so another account being busy enables the button',async()=>{
  const h=harness();await settle();
  assert.equal(h.el('cancel-dorm').disabled,true,'没有账号在忙时取消不可点');
  h.data.accounts.busy=true;await h.refresh();
  assert.equal(h.el('cancel-dorm').disabled,false,'当前账号闲着，忙着的是别的账号');
  h.el('cancel-dorm').listeners.click();await settle();await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)),[{action:'dorm_cancel',payload:{}}]);
  h.data.accounts.busy=false;h.data.dorm.busy=true;await h.refresh();
  assert.equal(h.el('cancel-dorm').disabled,false,'当前账号自己在忙时同样可以取消');
});

test('a snapshot without the per-account status fields still renders the bar',async()=>{
  const h=harness('windows',{accounts:{busy:undefined,items:[   // 老快照连 accounts.busy 都没有
    {id:'a1',name:'账号 1',active:true,idm_username:'2023123456'},
    {id:'a2',name:'账号 2',active:false,missing:true}]}});
  await settle();
  assert.deepEqual(h.el('dorm-account').options.map(option=>option.textContent),
                   ['账号 1 · 2023123456 · 未开启自动打卡','账号 2 · 档案缺失'],'缺字段一律按假值处理');
  assert.equal(h.el('account-state').textContent,'');
  assert.equal(h.el('cancel-dorm').disabled,true,'没有 busy 字段就是没人忙');
  for(const id of ACCOUNT_CONTROLS)assert.equal(h.el(id).disabled,false);
  h.el('account-add').onclick();await settle();await settle();
  assert.deepEqual(h.calls.map(call=>call.action),['account_add'],'账号栏照常能用');
});

/* 运行记录的滚动落点 --------------------------------------------------------------------------
   日志是升序的（最旧在上），人点开它多半想看「刚才那一下为什么失败」，也就是最后几行。 */
const LOG_STATUS=min=>`2026-10-10 11:${min}:01 INFO current auth status: authenticated`;
const LOG_ERROR=min=>`2026-10-10 11:${min}:05 ERROR login failed: 设备未注册,请在ePortal上添加认证设备`;
function recordsHarness(network=[LOG_STATUS('00'),LOG_ERROR('02')].join('\n')){
  return harness('windows',{logs:{network}});
}

test('opening the records page lands on the newest line instead of the oldest',async()=>{
  const h=recordsHarness('');
  h.navigate('records');await h.refresh();
  assert.equal(h.el('records').hidden,false);
  assert.equal(h.logView().showNewest,true,'打开这一页的意图就是看最新，先记下来');
  h.data.logs.network=LOG_ERROR('02');await h.refresh();
  assert.equal(h.el('log-output').scrollTop,h.el('log-output').scrollHeight,'打开就必须落在最新一行');
  assert.equal(h.el('log-jump').hidden,true,'已经在最新一行，不该出现提示');
  // 「刷新记录」和切换校园网 / 寝室打卡是同一类意图：直接给最新。
  h.scrolled(0);
  h.el('refresh-records').onclick();await h.refresh();
  assert.equal(h.el('log-output').scrollTop,h.el('log-output').scrollHeight,'点刷新后要回到最新一行');
  assert.equal(h.logView().unread,0);
});

test('reading older records is never interrupted, not even by the 1.5s snapshot',async()=>{
  const h=recordsHarness();
  h.navigate('records');await h.refresh();
  await h.refresh();
  h.scrolled(0);
  h.data.logs.network+='\n'+LOG_ERROR('10');
  await h.refresh();
  assert.equal(h.el('log-output').scrollTop,0,'正在翻旧记录时不能被拽回底部');
  assert.equal(h.el('log-jump').hidden,false,'有新记录时要出现提示');
  assert.equal(h.el('log-jump-count').textContent,'1','只算新增的那一条');
  await h.refresh();
  assert.equal(h.el('log-jump-count').textContent,'1','同一段内容再渲染一遍不能重复计数');
  assert.equal(h.el('log-output').scrollTop,0);
});

test('routine status lines never ring the unread counter',async()=>{
  const h=recordsHarness();
  h.navigate('records');await h.refresh();
  await h.refresh();
  h.scrolled(0);
  h.data.logs.network+='\n'+LOG_ERROR('11')+'\n'+LOG_STATUS('12')+'\n'+LOG_ERROR('13');
  await h.refresh();
  assert.equal(h.el('log-jump-count').textContent,'2','每分钟一条的 status 行不算新记录');
  assert.equal(h.el('log-output').scrollTop,0);
});

test('scrolling back to the bottom clears the prompt and resumes following',async()=>{
  const h=recordsHarness();
  h.navigate('records');await h.refresh();
  await h.refresh();
  h.scrolled(0);
  h.data.logs.network+='\n'+LOG_ERROR('10');
  await h.refresh();
  assert.equal(h.el('log-jump').hidden,false);
  h.scrolled(h.el('log-output').scrollHeight);
  assert.equal(h.el('log-jump').hidden,true,'回到底部后提示要收起');
  h.data.logs.network+='\n'+LOG_ERROR('11');
  await h.refresh();
  assert.equal(h.el('log-output').scrollTop,h.el('log-output').scrollHeight,'在底部时继续跟随最新一行');
});

test('the unread prompt jumps to the newest line when clicked',async()=>{
  const h=recordsHarness();
  h.navigate('records');await h.refresh();
  await h.refresh();
  h.scrolled(0);
  h.data.logs.network+='\n'+LOG_ERROR('10');
  await h.refresh();
  h.el('log-jump').onclick();
  assert.equal(h.el('log-output').scrollTop,h.el('log-output').scrollHeight,'点提示要回到底部');
  assert.equal(h.el('log-jump').hidden,true);
  assert.equal(h.logView().unread,0);
});

test('the records page still renders text and hides the prompt when there is nothing to show',async()=>{
  const h=recordsHarness('');
  h.navigate('records');await h.refresh();
  assert.equal(h.el('log-output').hidden,true);
  assert.equal(h.el('empty-records').hidden,false);
  assert.equal(h.el('log-jump').hidden,true,'没有日志时不该留着提示');
  h.data.logs.network=LOG_ERROR('02');
  await h.refresh();
  assert.equal(h.el('log-output').hidden,false);
  assert.equal(h.el('log-output').textContent,LOG_ERROR('02'),'日志照旧按纯文本渲染');
  assert.equal(h.el('empty-records').hidden,true);
});
