let snapshot = {runs:[],workers:[],vm:{},launch_errors:[],procedures:[]}, busy = false, selected = null, openWorkflow = null;
let launching=false;
const recording=recordingSwitch(()=>snapshot,renderComposer);
const ICONS = {
  local:'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
  vm:'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 18a4.5 4.5 0 0 1-.6-9 6 6 0 0 1 11.4 1.5A3.8 3.8 0 0 1 17.5 18z"/></svg>'
};
function target(){return document.querySelector('input[name="target"]:checked')?.value || 'local';}
function chosenKey(){return target()==='vm'?targetKey($('endpoint').value):'local';}
$('config-open').onclick=()=>{
  const settings=window.open('/config','echopath-config','popup=yes,width=720,height=850,resizable=yes,scrollbars=yes');
  if(settings)settings.focus();else showError('Allow pop-up windows for Control Room to open Config.');
};
function openVm(endpoint){
  const url='/vm?endpoint='+encodeURIComponent((endpoint||'').trim());
  const viewer=window.open(url,'echopath-vm-viewer','popup=yes,width=1200,height=800,resizable=yes,scrollbars=yes');
  if(viewer)viewer.focus();else showError('Allow pop-up windows for Control Room to open the VM.');
}

function desktops(){
  const vm=snapshot.vm||{}, list=['local'];
  if(vm.connected) list.push(targetKey(vm.endpoint));
  if(vm.reset&&!list.includes(targetKey(vm.reset.endpoint)))list.push(targetKey(vm.reset.endpoint));
  for(const r of snapshot.runs) if(!list.includes(r.target) && ACTIVE.has(r.status)) list.push(r.target);
  return list.map(t=>{
    const runs=snapshot.runs.filter(r=>r.target===t);
    return {target:t, active:runs.find(r=>ACTIVE.has(r.status)), last:runs[0]};
  });
}

function renderDesktops(){
  const list=desktops();
  if(!selected || !list.some(d=>d.target===selected)) selected=(list.find(d=>d.active)||list[0]).target;
  const box=$('desktops'), sig=list.map(d=>[d.target,d.active&&[d.active.id,d.active.status,d.active.detail],d.last&&d.last.id]).concat([selected, snapshot.vm&&snapshot.vm.platform, snapshot.vm?.reset]);
  if(!changed(box, sig)) return;
  box.replaceChildren();
  for(const d of list){
    const reset=snapshot.vm?.reset&&targetKey(snapshot.vm.reset.endpoint)===d.target?snapshot.vm.reset:null;
    const run=d.active, card=node('article','','desk'+(d.target===selected?' selected':''));
    const head=node('div','','desk-head'), icon=node('div','','desk-icon'); icon.innerHTML=d.target==='local'?ICONS.local:ICONS.vm;
    const names=node('div','','grow'); names.append(node('div',targetName(d.target,snapshot.vm),'desk-name'),
      node('div',d.target==='local'?'Local desktop':`${d.target.replace(/^https?:\/\//,'')}${snapshot.vm?.platform?' · '+snapshot.vm.platform:''}`,'desk-sub mono'));
    const resetBusy=reset&&reset.phase!=='ready'&&reset.phase!=='in_use';
    head.append(icon,names,resetBusy?node('span',reset.phase==='error'?'Reset failed':reset.phase==='awaiting_native'?'Waiting for continuation':'Resetting VM','badge '+(reset.phase==='error'?'t-bad':'t-warn')):run?badge(run.status):node('span','Idle','badge'));
    card.append(head);
    if(run){
      card.append(node('div',run.task,'task'),node('div',run.detail,'detail'));
      const meta=node('div','','meta'), time=node('span');time.dataset.started=run.started;
      meta.append(time,node('span','process '+run.pid)); card.append(meta);
      const actions=node('div','','actions');
      for(const [action,label,kind] of runActions(run)){const b=node('button',label,'btn '+kind);b.type='button';control(b,run.id,action,v=>{snapshot=v;showError('');render();});actions.append(b);}
      card.append(actions);
    } else {
      card.append(node('div',d.last?`Last: ${d.last.task}`:'No runs yet on this desktop.','detail'));
    }
    if(d.target!=='local'){const view=node('button','Open VM','btn small');view.type='button';view.onclick=()=>openVm(d.target);card.append(view);}
    if(reset){
      card.append(node('div',reset.phase==='ready'||reset.phase==='in_use'?'Restores the clean snapshot after each task.':reset.detail,'hint'+(reset.phase==='error'?' bad':'')));
      if(reset.phase==='error'){const retry=node('button','Retry reset','btn small');retry.type='button';retry.onclick=async()=>{retry.disabled=true;try{await api('/api/vm-reset',{endpoint:d.target});await refresh();}catch(e){showError(e.message);retry.disabled=false;}};card.append(retry);}
    }
    if(d.target!==selected){const view=node('button','View activity','btn link');view.type='button';view.onclick=()=>{selected=d.target;render();};card.append(view);}
    box.append(card);
  }
}

let showRaw=false;
function renderActivity(){
  const runs=snapshot.runs.filter(r=>r.target===selected), run=runs.find(r=>ACTIVE.has(r.status))||runs[0];
  $('activity-title').textContent='Activity · '+targetName(selected,snapshot.vm);
  const list=$('activity'), steps=(run&&run.steps)||[];
  $('raw-toggle').hidden=!steps.length;
  if(!changed(list,[run&&run.id,run&&run.events.length,run&&run.detail,steps,showRaw]))return;
  list.replaceChildren();
  list.className=steps.length&&!showRaw?'steps-log':'events';
  if(!run){list.append(node('li','No activity yet.','faint'));return;}
  if(steps.length&&!showRaw){steps.forEach((s,i)=>list.append(stepLine(s,false,steps[i+1])));return;}
  const events=run.events.slice(-40);
  if(!events.length) list.append(node('li',run.detail,'faint'));
  for(const e of events){const li=node('li');li.append(node('time',clock(e.timestamp)),node('span',e.detail));list.append(li);}
}
$('raw-toggle').onclick=()=>{showRaw=!showRaw;$('raw-toggle').textContent=showRaw?'Show steps':'Show raw events';$('raw-toggle').setAttribute('aria-pressed',String(showRaw));renderActivity();};

function renderRuns(){
  const rows=snapshot.runs.filter(r=>$('filter').value==='all'||ACTIVE.has(r.status)), body=$('runs');
  $('runs-empty').hidden=rows.length>0;
  if(!changed(body,[$('filter').value,rows.map(r=>[r.id,r.status,r.detail])]))return;
  body.replaceChildren();
  for(const r of rows.slice(0,50)){
    const tr=node('tr'), [label,tone]=statusOf(r.status), st=node('span','','status '+tone);
    st.append(node('span','','dot '+tone),label);
    const task=node('td'), open=node('button',r.task,'run-open task');open.type='button';
    open.setAttribute('aria-label','Show steps and trace for: '+r.task);open.onclick=()=>openRun(r.id);task.append(open);
    if(r.status==='error'||r.status==='interrupted')task.append(node('div',r.detail,'detail bad'));
    const dur=node('td','','mono nowrap');dur.dataset.started=r.started;if(!ACTIVE.has(r.status))dur.dataset.ended=r.updated;
    const first=node('td','','nowrap');first.append(st);
    tr.append(first,task,node('td',targetName(r.target,snapshot.vm),'nowrap'),dur,node('td',when(r.started),'nowrap faint'));
    body.append(tr);
  }
}

function renderComposer(){
  recording.render(snapshot);
  const vm=snapshot.vm||{}, busyMap=busyTargets(snapshot);
  if(vm.configured && vm.endpoint) $('endpoint').value=vm.endpoint;
  else if(vm.connected && !$('endpoint').value) $('endpoint').value=vm.endpoint;
  $('vm-label').textContent=vm.connected?`Virtual machine${vm.platform?' · '+vm.platform:''}`:'Virtual machine';
  const localBusy=busyMap.get('local'), vmBusy=busyMap.get(targetKey($('endpoint').value));
  $('busy-local').textContent=localBusy?'Busy · '+localBusy:'';
  $('busy-vm').textContent=$('endpoint').value&&vmBusy?'Busy · '+vmBusy:'';
  const radios=document.querySelectorAll('input[name="target"]');
  radios[0].disabled=!!localBusy;
  if(radios[0].checked&&localBusy&&!vmBusy){radios[1].checked=true;updateTarget();}
  $('start').disabled=recording.busy||launching||!!busyMap.get(chosenKey())||(target()==='vm'&&vm.enabled!==undefined&&!vm.connected);
  $('vm-open').disabled=!$('endpoint').value.trim();
}

// Run details: every step with its time, and the full event trace on request.
let openRunId=null, traceShown=false;
function openRun(id){openRunId=id;traceShown=false;$('run-trace').hidden=true;$('run-trace-toggle').textContent='Show';$('run-trace-toggle').setAttribute('aria-expanded','false');
  delete $('run-steps').dataset.sig;renderRunDialog();$('run-dialog').showModal();}
function renderRunDialog(){
  if(!openRunId)return;
  const r=snapshot.runs.find(x=>x.id===openRunId); if(!r)return;
  const steps=r.steps||[], [label,tone]=statusOf(r.status);
  $('run-title').textContent=r.task;
  const secs=steps.map((s,i)=>stepSeconds(s,steps[i+1])||0), slowest=steps.length?Math.max(...secs):0;
  const summary=[[label,'badge '+tone],[targetName(r.target,snapshot.vm),'chip'],[duration(elapsedSeconds(r)),'chip mono'],
    [steps.length+' steps','chip'],[steps.filter(s=>s.source==='replay').length+' replayed','chip'],
    [(n=>n+(n===1?' model call':' model calls'))(steps.reduce((a,s)=>a+(s.model_calls||0),0)),'chip'],[slowest?('slowest step '+slowest+' s'):'','chip'],[when(r.started),'chip']];
  const box=$('run-summary'); if(changed(box,summary)){box.replaceChildren(...summary.filter(x=>x[0]).map(([t,c])=>node('span',t,c)));}
  const video=$('run-video'), src=`/api/runs/${encodeURIComponent(r.id)}/recording`;
  $('run-video-box').hidden=!r.recording;
  if(r.recording&&video.dataset.src!==src){video.dataset.src=src;video.src=src;$('run-video-link').href=src;$('run-video-link').setAttribute('download',r.id+'.mp4');}
  $('run-detail').textContent=r.detail; $('run-detail').className='detail'+(tone==='t-bad'?' bad':'');
  $('run-step-count').textContent=steps.length?'':'No step details were recorded for this run.';
  const list=$('run-steps');
  if(changed(list,steps)){list.replaceChildren();steps.forEach((s,i)=>list.append(stepLine(s,false,steps[i+1])));}
  if(traceShown&&ACTIVE.has(r.status))loadTrace();
}
async function loadTrace(){
  try{const {trace}=await api(`/api/runs/${encodeURIComponent(openRunId)}/trace`), list=$('run-trace');
    if(!changed(list,[openRunId,trace.length]))return;
    list.replaceChildren(...trace.map(e=>{const li=node('li');li.append(node('time',e.timestamp?clock(e.timestamp):''),node('span',(e.event?e.event.replace(/^agent\./,'')+' · ':'')+e.detail));return li;}));
  }catch(e){showError(e.message);}
}
$('run-trace-toggle').onclick=()=>{traceShown=!traceShown;$('run-trace').hidden=!traceShown;$('run-trace-toggle').textContent=traceShown?'Hide':'Show';
  $('run-trace-toggle').setAttribute('aria-expanded',String(traceShown));if(traceShown){delete $('run-trace').dataset.sig;loadTrace();}};
$('run-close').onclick=()=>$('run-dialog').close();
$('run-dialog').addEventListener('close',()=>{openRunId=null;const v=$('run-video');v.pause();v.removeAttribute('src');delete v.dataset.src;v.load();});

function render(){renderDesktops();renderActivity();renderRuns();renderComposer();renderProcedures();renderRunDialog();tickElapsed();}

function updateTarget(){
  const vm=target()==='vm';$('vm').hidden=!vm;
  if(vm)$('desktop').checked=true;$('desktop').disabled=vm;
  $('target-note').textContent=vm?'Required for VM tasks. Your Mac stays free.':'Uses this Mac’s mouse and keyboard.';
  renderComposer();
}

function procKey(p){return p.store+'/'+p.id;}
function renderProcedures(){
  const procs=snapshot.procedures||[], list=$('wf-list');
  $('wf-count').textContent=procs.length?`${procs.length} stored`:'';
  if(!changed(list,procs.map(p=>[procKey(p),p.updated_at,p.status])))return;
  list.replaceChildren();
  if(!procs.length){list.append(node('p','No stored procedures yet. Successful runs are saved here automatically.','empty'));return;}
  for(const p of procs){
    const item=node('button','','wf-item');item.type='button';item.setAttribute('role','listitem');item.dataset.key=procKey(p);
    const store=p.store==='memory-repo'?'learned path':'replay library';
    item.append(node('span',p.title,'task'),node('span',`${p.steps} steps · ${store}${p.replayable?'':' · '+(p.status||'inactive')}`,'faint'));
    item.onclick=()=>showProcedure(p.store,p.id);
    list.append(item);
  }
  markCurrent();
}
function markCurrent(){for(const b of document.querySelectorAll('.wf-item'))b.setAttribute('aria-current',String(b.dataset.key===openWorkflow));}

async function showProcedure(store,id){
  openWorkflow=store+'/'+id;markCurrent();
  const box=$('wf-detail');
  let w;try{w=(await api(`/api/procedures/${encodeURIComponent(store)}/${encodeURIComponent(id)}`)).procedure;}catch(e){box.replaceChildren(node('p',e.message,'detail bad'));return;}
  if(openWorkflow!==store+'/'+id)return;
  box.replaceChildren();
  const head=node('div','','row');head.append(node('h3',w.title,'grow'));
  if(w.replayable&&w.query){const replay=node('button','Use as task','btn primary small');replay.type='button';
    replay.onclick=()=>{$('task').value=w.query;$('task').focus();$('task').scrollIntoView({behavior:'smooth',block:'center'});};head.append(replay);}
  else head.append(node('span',w.status||'inactive','badge plain'));
  box.append(head);
  if(w.summary)box.append(node('p',w.summary,'detail'));
  const chips=node('div','','chips');
  for(const c of [w.store==='memory-repo'?'learned path':'replay library',w.platform,w.updated_at&&('updated '+new Date(w.updated_at).toLocaleDateString())].concat(w.tags))if(c)chips.append(node('span',c,'chip'));
  box.append(chips);
  if(w.parameters.length){box.append(node('div','Parameters','label'));const dl=node('dl','','params');
    for(const p of w.parameters)dl.append(node('dt',p.name),node('dd',p.value));box.append(dl);}
  if(w.query)box.append(node('div','Replay phrase','label'),node('div',w.query,'query'));
  box.append(node('div',`Steps (${w.steps.length})`,'label'));
  const ol=node('ol','','steps');
  for(const s of w.steps){const li=node('li'),text=node('span');text.append(node('span',s.detail));if(s.expected)text.append(node('span',s.expected,'exp'));li.append(node('span',s.action,'act'),text);ol.append(li);}
  box.append(ol);
}

async function refresh(){
  if(busy)return;busy=true;
  try{snapshot=await api('/api/status');const c=$('connection');c.textContent='● Connected';c.className='conn ok';render();
    showLaunchError();}
  catch(e){const c=$('connection');c.textContent='Disconnected — run status unknown';c.className='conn bad';
    showError('Control Room is unreachable. Workers may still be running. Restart the dashboard to reconnect.');}
  finally{busy=false;}
}
// A worker that failed before starting its run is reported once, until dismissed.
let launchShown=0;
function dismissedAt(){try{return +localStorage.getItem('echopath.launchDismissed')||0;}catch(e){return 0;}}
function showLaunchError(){
  const last=snapshot.launch_errors.at(-1);
  if(!last||last.time<=dismissedAt()||last.time===launchShown)return;
  launchShown=last.time;showError('A task could not start:\n'+last.detail);
}
$('error-close').onclick=()=>{const last=snapshot.launch_errors.at(-1);if(last){try{localStorage.setItem('echopath.launchDismissed',String(last.time));}catch(e){}}showError('');};
$('filter').onchange=render;
$('endpoint').oninput=renderComposer;
$('vm-open').onclick=()=>openVm($('endpoint').value);
$('launch').onchange=e=>{if(e.target.name==='target')updateTarget();};
$('check').onclick=async()=>{try{const h=await api('/api/vm-health',{endpoint:$('endpoint').value});$('health').textContent=`Connected · ${h.platform||'guest desktop'} · ${h.auth_required?'authentication required':'tunnel-only connection'}`;snapshot.vm=h;refresh();}catch(e){$('health').textContent=e.message;}};
$('launch').onsubmit=async e=>{e.preventDefault();if(recording.busy||launching)return;launching=true;$('start').disabled=true;
  try{snapshot=await api('/api/run',{task:$('task').value,desktop:$('desktop').checked,endpoint:target()==='vm'?$('endpoint').value.trim():''});
    selected=chosenKey();showError('');$('task').value='';render();}
  catch(e){showError(e.message);}finally{launching=false;renderComposer();}};
setInterval(refresh,1000);setInterval(tickElapsed,1000);
refresh();
