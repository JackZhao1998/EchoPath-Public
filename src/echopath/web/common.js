// Shared by the Control Room and the menu-bar popover.
const $ = id => document.getElementById(id);
const ACTIVE = new Set(['queued','running','pausing','paused','stopping','resetting','awaiting_native']);
const STATUS = {running:['Running','t-run'], pausing:['Pausing','t-warn'], paused:['Paused','t-warn'], stopping:['Stopping','t-warn'],
  completed:['Done','t-ok'], stopped:['Stopped','t-mute'], error:['Failed','t-bad'], interrupted:['Interrupted','t-bad'],
  resetting:['Resetting VM','t-warn'], awaiting_native:['Waiting for continuation','t-warn']};
function statusOf(s){return STATUS[s] || [s ? s[0].toUpperCase()+s.slice(1) : 'Unknown','t-mute'];}
async function api(path, data) {
  const response=await fetch(path, data ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)} : {});
  const value=await response.json(); if(!response.ok) throw new Error(value.error || 'Request failed'); return value;
}
function node(tag, text, cls) {const n=document.createElement(tag); if(text!=null)n.textContent=text; if(cls)n.className=cls; return n;}
function badge(status){const [label,tone]=statusOf(status); return node('span',label,'badge '+tone);}
function targetKey(endpoint){return (endpoint||'').trim().replace(/\/+$/,'') || 'local';}
function targetName(target, vm){if(target==='local')return 'This computer'; return vm && targetKey(vm.endpoint)===target ? 'Virtual machine' : 'VM · '+target.replace(/^https?:\/\//,'');}
function elapsedSeconds(r){return Math.max(0,Math.floor((ACTIVE.has(r.status)?Date.now()/1000:r.updated)-r.started));}
function duration(sec){const m=Math.floor(sec/60), s=sec%60; return m>=60?`${Math.floor(m/60)}h ${m%60}m`:`${m}m ${String(s).padStart(2,'0')}s`;}
function clock(ts){return new Date(ts).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function when(sec){
  const d=new Date(sec*1000), now=new Date(), day=86400000, t=d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  if(d.toDateString()===now.toDateString())return 'Today '+t;
  if(d.toDateString()===new Date(now-day).toDateString())return 'Yesterday '+t;
  return (now-d<6*day)?d.toLocaleDateString([], {weekday:'short'})+' '+t:d.toLocaleDateString();
}
function busyTargets(snapshot){
  const busy=new Map();
  for(const r of snapshot.runs) if(ACTIVE.has(r.status)&&!busy.has(r.target)) busy.set(r.target, statusOf(r.status)[0].toLowerCase());
  for(const t of snapshot.workers||[]) if(!busy.has(t)) busy.set(t,'starting');
  const reset=snapshot.vm?.reset;
  if(reset&&reset.phase!=='ready'&&reset.phase!=='in_use')busy.set(targetKey(reset.endpoint),reset.phase==='error'?'reset failed':'resetting VM');
  return busy;
}
// Rebuild only when the data changed, so buttons keep focus and hover across 1 s polls.
function changed(el, value){const key=JSON.stringify(value); if(el.dataset.sig===key)return false; el.dataset.sig=key; return true;}
function tickElapsed(){for(const el of document.querySelectorAll('[data-started]')){const end=el.dataset.ended?+el.dataset.ended:Date.now()/1000; el.textContent=duration(Math.max(0,Math.floor(end-el.dataset.started)));}}
function control(button, id, action, after){
  button.onclick=async()=>{button.disabled=true;try{after(await api('/api/control',{id,action}));}catch(e){showError(e.message);button.disabled=false;}};
}
function showError(message){const el=$('error'); if(!el)return; el.hidden=!message; ($('error-text')||el).textContent=message||'';}
function recordingSwitch(getSnapshot,redraw){
  const input=$('recording-enabled'),control={busy:false,render(snapshot){
    input.disabled=control.busy||snapshot.recording_supported===false||typeof snapshot.recording_enabled!=='boolean';
    $('recording-note').textContent=snapshot.recording_supported===false?'Video recording currently requires a macOS host.':'Save a video of new VM tasks.';
    if(!control.busy)input.checked=!!snapshot.recording_enabled;
  }};
  input.onchange=async()=>{
    const enabled=input.checked;control.busy=true;redraw();
    try{const response=await api('/api/config/recording',{enabled});getSnapshot().recording_enabled=response.recording_enabled;showError('');}
    catch(error){showError(error.message);}
    finally{control.busy=false;redraw();}
  };
  return control;
}
function runActions(r){
  return r.status==='paused'?[['resume','Resume','primary'],['stop','Stop','danger']]
    :r.status==='running'?[['pause','Pause',''],['stop','Stop','danger']]
    :r.status==='pausing'?[['stop','Stop','danger']]:[];
}

// One readable line per desktop step (see run_steps.py); shared by the Control Room and the tray.
// A step lasts from when it began until the next one began (or its last update).
function stepSeconds(s,next){const a=Date.parse(s.started||s.timestamp),b=Date.parse((next&&(next.started||next.timestamp))||s.updated_at);return a&&b&&b>=a?Math.round((b-a)/1000):null;}
function stepMark(s){
  if(s.status==='completed'||s.verification_status==='succeeded')return ['✓','ok'];
  if(s.status==='error')return ['✗','bad'];
  if(s.status==='stopped')return ['–','run'];
  return ['…','run'];
}
function stepAction(s){
  const action=s.action||'Deciding the next action…';
  // "Click at (770, 56)" says nothing; the planner's target does.
  const m=/^(Double-click|Click|Move to)\b/.exec(action);
  return m&&s.target?`${m[1]} ${s.target}`:action;
}
function stepLine(s, compact, next){
  const li=node('li','','step-line'), head=node('div','','step-head'), [mark,tone]=stepMark(s);
  head.append(node('span',mark,'step-mark '+tone),node('span','Step '+s.n,'step-n'),
    node('span',s.source==='replay'?'Replayed':'Planned','badge plain '+(s.source==='replay'?'t-ok':'')));
  const secs=stepSeconds(s,next), meta=[secs!=null?secs+' s':null, s.model_calls?s.model_calls+(s.model_calls===1?' model call':' model calls'):null].filter(Boolean).join(' · ');
  head.append(node('span','','grow'),node('span',meta,'faint'));
  li.append(head,node('div',stepAction(s),'step-action'));
  if(!compact){
    if(s.expected_outcome)li.append(node('div','Expect: '+s.expected_outcome,'hint'));
    const result=s.blocker||s.verification;
    if(result)li.append(node('div',result,'step-result '+tone));
  }
  if(s.notes&&s.notes.length){const notes=node('div','','chips');for(const n of s.notes)notes.append(node('span',n,'chip'));li.append(notes);}
  return li;
}
