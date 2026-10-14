const el=id=>document.getElementById(id);
const endpoint=(new URLSearchParams(location.search).get('endpoint')||'').trim();
const stage=el('viewer-stage');
let paused=false,timer=null,request=null,imageUrl=null,closed=false,live=false;
let controlling=false,availability={allowed:false},statusTimer=null,statusRequest=false;
let queue=[],sending=false,clickTimer=null,pendingClick=null,pointer=null,dragged=false,lastMove=0;
el('viewer-endpoint').textContent=endpoint;

function status(label,tone=''){
  el('viewer-status').textContent=label;el('viewer-status').className='badge '+tone;
}
function inputError(message=''){
  el('viewer-input-error').textContent=message;el('viewer-input-error').hidden=!message;
}
function canControl(){return controlling&&availability.allowed&&live&&!paused&&!closed&&!document.hidden;}
function cancelClick(){clearTimeout(clickTimer);pendingClick=null;}
function flushClick(){const action=pendingClick;cancelClick();if(action)enqueue(action);}
function releaseControl(){controlling=false;queue=[];cancelClick();pointer=null;renderControls();}
function renderControls(){
  const task=availability.task;
  el('viewer-task').hidden=!task;
  const outcome={completed:'Completed',error:'Failed',stopped:'Stopped',interrupted:'Interrupted'}[task?.status]||'Last task';
  el('viewer-task-title').textContent=task?(availability.run_id?'Current task: ':outcome+': ')+task.title:'';
  const detail=task?.detail||'';
  el('viewer-task-detail').textContent=detail.startsWith('Calling model router')?'Waiting for the model to choose the next action…':detail;
  el('viewer-task-detail').title=detail;
  const ready=availability.allowed&&live&&!paused;
  el('viewer-control').disabled=!ready;
  el('viewer-control').textContent=controlling?'Release control':'Control VM';
  el('viewer-control').setAttribute('aria-pressed',String(controlling));
  for(const id of ['viewer-text','viewer-key','viewer-send-key'])el(id).disabled=!canControl();
  stage.classList.toggle('controlling',canControl());
  el('viewer-mode').textContent=canControl()?'Mouse and keyboard control':availability.run_status==='running'?'Task controls the VM':'View only';
  el('viewer-input-hint').textContent=!availability.allowed?availability.detail||'Checking desktop control…'
    :paused?'Resume the live view to control the VM.'
    :controlling?'Click, double-click, right-click, drag or scroll. Click the desktop to type.'
    :availability.platform==='linux'?'Select Control VM to use the desktop and its apps. Ctrl + Alt + T opens a terminal.':'Select Control VM to use the guest desktop.';
  el('viewer-task-pause').hidden=!availability.run_id||availability.run_status!=='running';
}
async function post(path,data){
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),12000);
  try{
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),signal:controller.signal});
    const result=await response.json();if(!response.ok)throw new Error(result.error||'The VM request failed.');return result;
  }catch(error){throw error.name==='AbortError'?new Error('The VM request timed out.'):error;}
  finally{clearTimeout(timeout);}
}
async function checkControl(){
  if(statusRequest||closed||!endpoint||document.hidden)return;
  statusRequest=true;
  try{
    availability=await post('/api/vm-input-status',{endpoint});
    if(!availability.allowed&&controlling)releaseControl();
  }catch(error){availability={allowed:false,detail:error.message};releaseControl();}
  finally{statusRequest=false;renderControls();}
}
function schedule(delay){
  clearTimeout(timer);if(endpoint&&!closed&&!paused&&!document.hidden)timer=setTimeout(refresh,delay);
}
async function refresh(){
  if(closed||paused||document.hidden||request)return;
  const controller=new AbortController();request=controller;
  const timeout=setTimeout(()=>controller.abort(),12000);
  let delay=controlling?300:1000,nextUrl=null;
  try{
    const response=await fetch('/api/vm-frame',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint}),signal:controller.signal,cache:'no-store',
    });
    if(!response.ok){const error=await response.json();throw new Error(error.error||'The VM screen is unavailable.');}
    nextUrl=URL.createObjectURL(await response.blob());
    const decoded=new Image();decoded.src=nextUrl;await decoded.decode();
    if(closed||paused||document.hidden)return;
    const previous=imageUrl;imageUrl=nextUrl;nextUrl=null;
    const screen=el('viewer-screen');screen.src=imageUrl;screen.hidden=false;
    if(previous)URL.revokeObjectURL(previous);
    live=true;el('viewer-empty').hidden=true;el('viewer-error').hidden=true;
    el('viewer-updated').textContent='Updated '+new Date().toLocaleTimeString();
    status('Live','t-ok');renderControls();
  }catch(error){
    live=false;releaseControl();
    if(!closed&&!paused&&!document.hidden){
      status('Reconnecting…','t-warn');
      el('viewer-error').textContent=(error.name==='AbortError'?'The VM took too long to respond.':error.message)+' Retrying…';
      el('viewer-error').hidden=false;el('viewer-empty').textContent='Waiting for the VM connection…';delay=3000;
    }
  }finally{
    if(nextUrl)URL.revokeObjectURL(nextUrl);
    clearTimeout(timeout);request=null;schedule(delay);
  }
}
function enqueue(action){
  if(!canControl())return;
  // Keep the focusing click ahead of text typed during double-click detection.
  if(pendingClick&&action.action!=='move')flushClick();
  const last=queue.at(-1);
  if(action.action==='move'&&last?.action==='move')queue[queue.length-1]=action;
  else if(action.action==='text'&&last?.action==='text'&&last.text.length+action.text.length<=4000)last.text+=action.text;
  else if(queue.length<64)queue.push(action);
  else {inputError('Input is arriving too quickly. Wait for the VM to catch up.');releaseControl();return;}
  drain();
}
async function drain(){
  if(sending||!canControl())return;
  sending=true;
  try{
    while(queue.length&&canControl()){
      const input=queue.shift();
      await post('/api/vm-input',{endpoint,input});
      schedule(0);
    }
  }catch(error){
    releaseControl();inputError(error.message+' Check the desktop before trying again.');checkControl();
  }finally{sending=false;}
}
// object-fit:contain leaves letterbox margins. They must never become guest clicks.
function point(event){
  const image=el('viewer-screen'),rect=image.getBoundingClientRect();
  const scale=Math.min(rect.width/image.naturalWidth,rect.height/image.naturalHeight);
  const width=image.naturalWidth*scale,height=image.naturalHeight*scale;
  const x=event.clientX-rect.left-(rect.width-width)/2,y=event.clientY-rect.top-(rect.height-height)/2;
  if(!width||!height||x<0||y<0||x>width||y>height)return null;
  return {x:x/width,y:y/height};
}
el('viewer-control').onclick=()=>{
  if(controlling)releaseControl();
  else if(availability.allowed&&live&&!paused){controlling=true;inputError();renderControls();stage.focus();schedule(0);}
};
el('viewer-task-pause').onclick=async()=>{
  const button=el('viewer-task-pause');button.disabled=true;
  try{await post('/api/control',{id:availability.run_id,action:'pause'});await checkControl();}
  catch(error){inputError(error.message);}finally{button.disabled=false;}
};
stage.addEventListener('pointerdown',event=>{
  if(!canControl())return;
  const p=point(event);if(!p)return;
  event.preventDefault();stage.focus();dragged=false;
  if(event.button===0){pointer={...p,clientX:event.clientX,clientY:event.clientY,id:event.pointerId};stage.setPointerCapture(event.pointerId);}
});
stage.addEventListener('pointerup',event=>{
  if(!pointer)return;
  const start=pointer;pointer=null;
  if(stage.hasPointerCapture(event.pointerId))stage.releasePointerCapture(event.pointerId);
  if(!canControl())return;
  const finish=point(event);
  if(finish&&Math.hypot(event.clientX-start.clientX,event.clientY-start.clientY)>4){
    dragged=true;enqueue({action:'drag',x:start.x,y:start.y,to_x:finish.x,to_y:finish.y,button:'left'});
  }
});
stage.addEventListener('pointercancel',()=>{pointer=null;});
stage.addEventListener('click',event=>{
  if(!canControl()||event.button!==0)return;
  if(dragged){dragged=false;return;}
  const p=point(event);if(!p)return;
  if(event.detail<2){flushClick();pendingClick={action:'click',...p,button:'left',clicks:1};clickTimer=setTimeout(flushClick,250);}
  else cancelClick();
});
stage.addEventListener('dblclick',event=>{
  if(!canControl())return;event.preventDefault();cancelClick();
  const p=point(event);if(p)enqueue({action:'click',...p,button:'left',clicks:2});
});
stage.addEventListener('contextmenu',event=>{
  if(!canControl())return;event.preventDefault();const p=point(event);if(p)enqueue({action:'click',...p,button:'right',clicks:1});
});
stage.addEventListener('auxclick',event=>{
  if(!canControl()||event.button!==1)return;event.preventDefault();const p=point(event);if(p)enqueue({action:'click',...p,button:'middle',clicks:1});
});
stage.addEventListener('pointermove',event=>{
  if(!canControl()||event.buttons||Date.now()-lastMove<200)return;
  const p=point(event);if(p){lastMove=Date.now();enqueue({action:'move',...p});}
});
stage.addEventListener('wheel',event=>{
  if(!canControl())return;event.preventDefault();const p=point(event);
  if(p&&event.deltaY)enqueue({action:'scroll',...p,clicks:-Math.sign(event.deltaY)*Math.min(10,Math.max(1,Math.ceil(Math.abs(event.deltaY)/(event.deltaMode===0?100:3))))});
},{passive:false});
const special={Enter:'enter',Tab:'tab',Escape:'esc',Backspace:'backspace',Delete:'delete',Insert:'insert',Home:'home',End:'end',PageUp:'pageup',PageDown:'pagedown',ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right',' ':'space',CapsLock:'capslock',PrintScreen:'printscreen'};
stage.addEventListener('keydown',event=>{
  if(!canControl())return;
  if(['Control','Alt','Shift','Meta','Dead','Process'].includes(event.key)||event.isComposing)return;
  if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='v'&&!event.altKey)return; // user-initiated paste event below
  event.preventDefault();
  if(event.key.length===1&&!event.ctrlKey&&!event.altKey&&!event.metaKey){enqueue({action:'text',text:event.key});return;}
  const key=special[event.key]||(/^F([1-9]|1[0-2])$/.test(event.key)?event.key.toLowerCase():event.key.toLowerCase());
  const keys=[];if(event.ctrlKey)keys.push('ctrl');if(event.metaKey)keys.push(availability.platform==='darwin'?'command':availability.platform==='win32'?'win':'ctrl');
  if(event.altKey)keys.push('alt');if(event.shiftKey)keys.push('shift');keys.push(key);
  enqueue({action:'key',keys:[...new Set(keys)]});
});
stage.addEventListener('paste',event=>{
  if(!canControl())return;event.preventDefault();const text=event.clipboardData.getData('text/plain');
  if(text.length>4000){inputError('Paste up to 4,000 characters at a time.');return;}if(text)enqueue({action:'text',text});
});
el('viewer-send-key').onclick=()=>{enqueue({action:'key',keys:el('viewer-key').value.split(',')});stage.focus();};
el('viewer-text').onclick=()=>{el('viewer-text-dialog').showModal();el('viewer-text-value').focus();};
el('viewer-text-cancel').onclick=()=>{el('viewer-text-dialog').close();stage.focus();};
el('viewer-text-form').onsubmit=event=>{
  event.preventDefault();const text=el('viewer-text-value').value;if(!text)return;
  enqueue({action:'text',text});el('viewer-text-value').value='';el('viewer-text-dialog').close();stage.focus();
};
el('viewer-pause').onclick=()=>{
  paused=!paused;el('viewer-pause').textContent=paused?'Resume view':'Pause view';el('viewer-pause').setAttribute('aria-pressed',String(paused));
  if(paused){releaseControl();clearTimeout(timer);request?.abort();el('viewer-error').hidden=true;status('View paused','t-warn');}
  else {status('Connecting…');schedule(0);}renderControls();
};
el('viewer-fullscreen').onclick=async()=>{
  try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen();}
  catch{inputError('Full screen is unavailable in this browser. You can resize the window.');}
};
document.addEventListener('fullscreenchange',()=>{el('viewer-fullscreen').textContent=document.fullscreenElement?'Exit full screen':'Full screen';});
document.addEventListener('visibilitychange',()=>{
  if(document.hidden){releaseControl();clearTimeout(timer);request?.abort();if(!paused)status('View suspended');}
  else {checkControl();if(!paused){status('Connecting…');schedule(0);}}
});
window.addEventListener('pagehide',()=>{closed=true;releaseControl();clearTimeout(timer);clearInterval(statusTimer);request?.abort();if(imageUrl)URL.revokeObjectURL(imageUrl);});
function start(){closed=false;checkControl();clearInterval(statusTimer);statusTimer=setInterval(checkControl,1000);schedule(0);}
window.addEventListener('pageshow',start);
if(!endpoint){el('viewer-error').textContent='Open this viewer from a VM in the Control Room.';el('viewer-error').hidden=false;status('No VM selected','t-warn');}
else start();
