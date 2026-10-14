// Menu-bar popover. Native actions (open browser, quit) go through the WebKit bridge in tray.py.
let snapshot = {runs:[],workers:[],vm:{},launch_errors:[],procedures:[]}, busy = false;
let launching=false;
const recording=recordingSwitch(()=>snapshot,render);
const bridge = window.webkit?.messageHandlers?.echopath;
function native(message){if(bridge)bridge.postMessage(message);else if(message.startsWith('open'))window.open('/'+message.slice(5));}

function vmButton(r){
  if(!r.target||r.target==='local')return null;
  const button=node('button','Open VM','btn');button.type='button';
  button.onclick=()=>{
    if(bridge){native('open:vm:'+r.id);return;}
    const viewer=window.open('/vm?endpoint='+encodeURIComponent(r.target),'echopath-vm-viewer','popup=yes,width=1200,height=800,resizable=yes,scrollbars=yes');
    if(viewer)viewer.focus();else showError('Allow pop-up windows for EchoPath to open the VM.');
  };
  return button;
}

function overall(active){
  const states=new Set(active.map(r=>r.status));
  if(!active.length)return ['Idle',''];
  if(states.has('resetting'))return ['Resetting VM','t-warn'];
  if(states.has('awaiting_native'))return ['Waiting','t-warn'];
  if([...states].every(s=>s==='paused'))return ['Paused','t-warn'];
  if(states.has('pausing'))return ['Pausing','t-warn'];
  if(states.has('running'))return ['Running','t-run'];
  return ['Stopping','t-warn'];
}

function runCard(r){
  const card=node('div','','card');
  card.append(node('div','Current task','label'),node('div',r.task,'task'));
  const chips=node('div','','chips');chips.append(node('span',targetName(r.target,snapshot.vm),'chip'),badge(r.status));card.append(chips);
  card.append(node('div',r.detail,'detail'));
  const steps=r.steps||[];
  if(steps.length){const ol=node('ol','','steps-log compact');const last=steps.slice(-3);last.forEach((s,i)=>ol.append(stepLine(s,true,last[i+1])));card.append(ol);}
  else{const events=r.events.slice(-4);
    if(events.length){const ol=node('ol','','events');for(const e of events){const li=node('li');li.append(node('time',clock(e.timestamp)),node('span',e.detail));ol.append(li);}card.append(ol);}}
  const meta=node('div','','meta'),time=node('span');time.dataset.started=r.started;meta.append(time,node('span','process '+r.pid));card.append(meta);
  const actions=node('div','','actions');
  const view=vmButton(r);if(view)actions.append(view);
  for(const [action,label,kind] of runActions(r)){const b=node('button',label,'btn '+kind);b.type='button';control(b,r.id,action,v=>{snapshot=v;showError('');render();});actions.append(b);}
  if(actions.childElementCount)card.append(actions);
  return card;
}

function recentCard(r){
  const [label,tone]=statusOf(r.status), card=node('div','','card');
  const head=node('div','','row'),title=node('div','','grow');
  title.append(node('div',{completed:'Finished',stopped:'Stopped',error:'Failed',interrupted:'Interrupted'}[r.status]||label,'task'),node('div',r.task,'hint ellipsis'));
  head.append(title,badge(r.status));card.append(head);
  card.append(node('div',r.detail,'detail clamp'+(tone==='t-bad'?' bad':'')));
  const meta=node('div','','meta'),time=node('span');time.dataset.started=r.started;time.dataset.ended=r.updated;
  meta.append(time,node('span',targetName(r.target,snapshot.vm)));card.append(meta);
  const view=vmButton(r);if(view)card.append(view);
  return card;
}

function render(){
  recording.render(snapshot);$('start').disabled=recording.busy||launching;
  const active=snapshot.runs.filter(r=>ACTIVE.has(r.status)), [label,tone]=overall(active);
  const pill=$('pill');pill.textContent=label;pill.className='badge '+tone;
  $('banner').hidden=!active.some(r=>r.target==='local'&&(r.status==='paused'||r.status==='pausing'));
  $('vm-note').hidden=!active.some(r=>r.target!=='local'&&r.status==='running');
  const runs=$('runs');
  if(changed(runs,active.map(r=>[r.id,r.status,r.detail,r.events.length,r.steps]))){runs.replaceChildren(...active.map(runCard));}
  const last=snapshot.runs[0], recent=$('recent');
  const showRecent=!active.length&&last&&Date.now()/1000-last.updated<600;
  recent.hidden=!showRecent;
  if(showRecent&&changed(recent,[last.id,last.status]))recent.replaceChildren(recentCard(last));
  // A new task is offered only while some desktop is free.
  const busyMap=busyTargets(snapshot), vm=snapshot.vm||{};
  $('vm-radio').disabled=!vm.connected||busyMap.has(targetKey(vm.endpoint));
  document.querySelector('input[value="local"]').disabled=busyMap.has('local');
  const free=!busyMap.has('local')||(vm.connected&&!busyMap.has(targetKey(vm.endpoint)));
  $('launch').hidden=!free;renderProcedures();$('wf').hidden=!free||!$('wf-list').childElementCount;
  const checked=document.querySelector('input[name="target"]:checked');
  if(checked?.disabled){const other=[...document.querySelectorAll('input[name="target"]')].find(i=>!i.disabled);if(other){other.checked=true;updateTarget();}}
  tickElapsed();
}

function updateTarget(){
  const vm=document.querySelector('input[name="target"]:checked')?.value==='vm';
  if(vm)$('desktop').checked=true;$('desktop').disabled=vm;updateNote();
}
function updateNote(){
  const vm=document.querySelector('input[name="target"]:checked')?.value==='vm';
  $('desktop-note').textContent=vm?'Required for VM tasks. Your Mac stays free.':$('desktop').checked?'On: EchoPath uses this Mac’s mouse and keyboard.':'Off: EchoPath plans but won’t touch your screen.';
}

function renderProcedures(){
  const procs=(snapshot.procedures||[]).filter(p=>p.replayable).slice(0,3), list=$('wf-list');
  if(!changed(list,procs.map(p=>[p.store,p.id])))return;
  list.replaceChildren();
  for(const p of procs){
    const row=node('div'),text=node('div','','grow');text.append(node('div',p.title,'task ellipsis'),node('div',`${p.steps} steps`,'hint'));
    const use=node('button','Use','btn small');use.type='button';use.setAttribute('aria-label','Use '+p.title+' as the new task');
    use.onclick=async()=>{try{const w=(await api(`/api/procedures/${encodeURIComponent(p.store)}/${encodeURIComponent(p.id)}`)).procedure;$('task').value=w.query;$('task').focus();}catch(e){showError(e.message);}};
    row.append(text,use);list.append(row);
  }
}

async function refresh(){
  if(busy)return;busy=true;
  try{snapshot=await api('/api/status');showError('');render();}
  catch(e){$('pill').textContent='Offline';$('pill').className='badge t-bad';showError('Control Room is unreachable. Workers may still be running.');}
  finally{busy=false;}
}
// Inside the menu-bar app the page stays loaded while hidden; poll only while it is shown.
let poll=null;
window.trayOpened=()=>{refresh();if(!poll)poll=setInterval(()=>{refresh();tickElapsed();},1000);};
window.trayClosed=()=>{clearInterval(poll);poll=null;};
$('launch').onchange=e=>{if(e.target.name==='target')updateTarget();else if(e.target.id==='desktop')updateNote();};
$('launch').onsubmit=async e=>{e.preventDefault();if(recording.busy||launching)return;launching=true;$('start').disabled=true;
  const vm=document.querySelector('input[name="target"]:checked').value==='vm';
  try{snapshot=await api('/api/run',{task:$('task').value,desktop:$('desktop').checked,endpoint:vm?snapshot.vm.endpoint:''});$('task').value='';showError('');render();}
  catch(e){showError(e.message);}finally{launching=false;render();}};
$('open').onclick=()=>native('open');
$('config-open').onclick=()=>native('open:config');
$('wf-all').onclick=()=>native('open:#workflows');
$('quit').onclick=()=>native('quit');
$('quit').hidden=!bridge;
refresh();
if(!bridge)window.trayOpened();
