const el=id=>document.getElementById(id);
let settings=null,tokenBusy=false,recordingBusy=false;
let vmState=null,vmBusy=false,vmLoaded=false,vmDirty=false,vmJobSignature='';
async function request(path,data){
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),65000);
  try{
    const response=await fetch(path,{...(data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}),signal:controller.signal,cache:'no-store'});
    const value=await response.json();if(!response.ok)throw new Error(value.error||'The request failed.');return value;
  }catch(error){throw error.name==='AbortError'?new Error('The request timed out. Check the connection and try again.'):error;}
  finally{clearTimeout(timeout);}
}
function result(id,message,ok=null){const target=el(id);target.textContent=message;target.className='config-result'+(ok===true?' result-ok':ok===false?' result-bad':'');}
function render(){
  if(!settings)return;
  if(settings.vm){vmState=settings.vm;renderVM();}
  el('recording-enabled').disabled=recordingBusy||settings.recording_supported===false;
  el('recording-note').textContent=settings.recording_supported===false?'Video recording currently requires a macOS host. Task history is still saved.':'Save a video of new VM tasks. Changes apply to the next task.';
  if(!recordingBusy)el('recording-enabled').checked=settings.recording_enabled;
  const token=settings.openrouter,environment=token.source==='environment';
  const badge=el('token-badge');badge.textContent=token.valid===true?'Verified':!token.present?'Not configured':token.valid===false?'Needs attention':'Not verified';
  badge.className='badge '+(token.valid===true?'t-ok':token.valid===false?'t-warn':'');
  el('openrouter-token').disabled=tokenBusy||environment;
  el('openrouter-token').placeholder=token.present?'Enter a replacement token':'Paste an OpenRouter API token';
  el('token-storage').textContent=environment?`Managed by ${token.env_name}. Update that environment variable and restart EchoPath to replace it.`:'Stored locally in your Git-ignored credential file. The saved token is never displayed.';
  el('token-save').disabled=tokenBusy||environment||!el('openrouter-token').value.trim();
  el('token-test').disabled=tokenBusy||!token.present;
  el('token-remove').disabled=tokenBusy||environment||!token.present;
  el('token-test').textContent=environment?'Test token':'Test saved token';
  el('jev-enabled').checked=settings.jev_enabled;
  el('jev-enabled').disabled=settings.jev_supported===false||tokenBusy||(!settings.jev_enabled&&token.valid!==true);
  el('jev-note').textContent=settings.jev_supported===false?'JEV currently requires macOS text recognition.':token.valid===true?'Ready. Choose whether to use JEV for new tasks.':'Disabled until a valid OpenRouter token is available.';
}
async function tokenAction(path,data,message){
  tokenBusy=true;render();result('token-result',message);
  try{
    const response=await request(path,data);settings=response.settings;
    if(response.ok&&path.endsWith('/save'))el('openrouter-token').value='';
    result('token-result',response.message,response.ok);
    result('jev-result','');
  }catch(error){result('token-result',error.message,false);}
  finally{tokenBusy=false;render();}
}
el('openrouter-token').oninput=render;
el('recording-enabled').onchange=async event=>{
  const enabled=event.target.checked;recordingBusy=true;render();result('recording-result','Saving…');
  try{const response=await request('/api/config/recording',{enabled});settings.recording_enabled=response.recording_enabled;result('recording-result',response.message,response.ok);}
  catch(error){result('recording-result',error.message,false);}
  finally{recordingBusy=false;render();}
};
el('token-form').onsubmit=event=>{event.preventDefault();if(!el('token-save').disabled)tokenAction('/api/config/openrouter/save',{token:el('openrouter-token').value},'Verifying token with OpenRouter…');};
el('token-test').onclick=()=>tokenAction('/api/config/openrouter/test',{},'Verifying token with OpenRouter…');
el('token-remove').onclick=()=>tokenAction('/api/config/openrouter/remove',{},'Removing saved token…');
el('jev-enabled').onchange=async event=>{
  const enabled=event.target.checked;tokenBusy=true;render();result('jev-result',enabled?'Enabling JEV…':'Disabling JEV…');
  try{const response=await request('/api/config/jev',{enabled});settings=response.settings;result('jev-result',response.message,response.ok);}
  catch(error){result('jev-result',error.message,false);}
  finally{tokenBusy=false;render();}
};
for(const name of ['codex','claude'])el(name+'-test').onclick=async()=>{
  const button=el(name+'-test'),label=button.textContent;button.disabled=true;button.textContent='Testing…';result(name+'-result','Waiting for a reply through the bridge…');
  try{const response=await request('/api/config/bridge-test',{bridge:name});result(name+'-result',response.message,response.ok);}
  catch(error){result(name+'-result',error.message,false);}
  finally{button.disabled=false;button.textContent=label;}
};
async function load(){
  try{settings=await request('/api/config');render();result('token-result',settings.openrouter.message,settings.openrouter.valid);if(settings.openrouter.present)await tokenAction('/api/config/openrouter/test',{},'Verifying the configured token…');}
  catch(error){el('config-error').textContent=error.message;el('config-error').hidden=false;}
}
load();
window.addEventListener('focus',async()=>{
  if(tokenBusy||recordingBusy)return;
  try{const latest=await request('/api/config');if(!tokenBusy&&!recordingBusy){settings=latest;render();}}
  catch(error){/* Keep the current settings while the app reconnects. */}
});

function vmMode(){
  const mode=el('vm-mode').value;
  el('vm-ssh-fields').hidden=mode!=='ssh';
  el('vm-lima-fields').hidden=mode!=='lima';
  el('vm-virtualbox-fields').hidden=mode!=='virtualbox';
  el('vm-direct-fields').hidden=mode!=='direct';
  el('vm-tunnel-fields').hidden=mode==='direct';
  el('vm-display-fields').hidden=mode!=='ssh';
}
function fillVM(profile){
  const p=profile||{},ssh=p.ssh||{};
  const values={'vm-mode':p.mode||'ssh','vm-name':p.name||'My VM','vm-host':ssh.host||'',
    'vm-user':ssh.user||'','vm-port':ssh.port||'','vm-identity':ssh.identity_file||'',
    'vm-instance':p.instance||'echopath-desktop','vm-cpus':p.cpus||4,'vm-memory':p.memory_gib||4,
    'vm-disk':p.disk_gib||30,'vm-endpoint':p.mode==='direct'?p.endpoint||'':'',
    'vm-local-port':p.local_port||8791,'vm-bridge-port':p.bridge_port||8790,
    'vm-display':p.mode==='lima'?':0':p.display||':0','vm-xauthority':p.xauthority||'',
    'vm-windows-iso':p.iso_path||'','vm-windows-instance':p.mode==='virtualbox'?p.instance:'echopath-windows',
    'vm-windows-cpus':p.mode==='virtualbox'?p.cpus:4,'vm-windows-memory':p.mode==='virtualbox'?p.memory_gib:8,
    'vm-windows-disk':p.mode==='virtualbox'?p.disk_gib:80};
  for(const [id,value] of Object.entries(values))el(id).value=value;
  el('vm-reconnect').checked=p.auto_reconnect!==false;
  vmDirty=false;vmMode();
}
function renderVM(){
  if(!vmState)return;
  if(!vmLoaded){fillVM(vmState.profile);vmLoaded=true;}
  const busy=vmBusy||!!vmState.job?.running,locked=busy||vmState.enabled,profile=vmState.profile;
  const badge=el('vm-badge');badge.textContent=busy?'Setting up':!vmState.enabled?'Off':vmState.connected?'Ready':'Needs attention';
  badge.className='badge '+(vmState.connected?'t-ok':vmState.enabled?'t-warn':'');
  el('vm-status').textContent=(profile?profile.name+' · ':'')+vmState.message;
  el('vm-enabled').checked=vmState.enabled;
  el('vm-enabled').disabled=busy||vmDirty||!profile;
  for(const input of el('vm-form').querySelectorAll('input,select,button'))input.disabled=locked;
  el('vm-mode').querySelector('option[value="lima"]').disabled=vmState.host_platform!=='darwin';
  el('vm-mode').querySelector('option[value="virtualbox"]').disabled=vmState.host_platform!=='win32';
  el('vm-edit-note').textContent=vmState.enabled?'Turn off VM integration to edit.':vmDirty?'Save changes before connecting.':'';
  el('vm-key-note').textContent=vmState.key_saved?'A key is saved for this connection. Leave blank to keep it.':'Installing the guest bridge generates a key. For an existing bridge, enter its key here.';
  const unavailable=busy||vmDirty||!profile;
  el('vm-test').disabled=unavailable;
  el('vm-test').textContent=profile?.mode==='direct'?'Test desktop':profile?.mode==='virtualbox'?'Test VM':'Test SSH';
  el('vm-install').hidden=profile?.mode==='direct'||profile?.mode==='lima';
  el('vm-install').disabled=unavailable||vmState.enabled;
  el('vm-create').hidden=!['lima','virtualbox'].includes(profile?.mode);
  el('vm-create').textContent=vmState.managed_created?'Prepare VM & connect':'Create VM & connect';
  el('vm-create').disabled=unavailable||vmState.enabled||!vmState.can_create;
  el('vm-connect').disabled=unavailable||vmState.connected;
  el('vm-open').disabled=!vmState.connected;
  el('vm-window').hidden=profile?.mode!=='virtualbox';
  el('vm-window').disabled=!vmState.managed_created;
  el('vm-disconnect').disabled=busy||!vmState.enabled;
  const signature=JSON.stringify(vmState.job||{});
  if(signature!==vmJobSignature){vmJobSignature=signature;if(vmState.job?.message)result('vm-result',vmState.job.message,vmState.job.running?null:vmState.job.ok);}
}
function vmProfile(){
  return {id:vmState?.profile?.id||'default',name:el('vm-name').value,mode:el('vm-mode').value,
    ssh:{host:el('vm-host').value,user:el('vm-user').value,port:el('vm-port').value,identity_file:el('vm-identity').value},
    endpoint:el('vm-endpoint').value,local_port:el('vm-local-port').value,bridge_port:el('vm-bridge-port').value,
    display:el('vm-display').value,xauthority:el('vm-xauthority').value,auto_reconnect:el('vm-reconnect').checked,
    iso_path:el('vm-windows-iso').value,
    instance:el('vm-mode').value==='virtualbox'?el('vm-windows-instance').value:el('vm-instance').value,
    cpus:el(el('vm-mode').value==='virtualbox'?'vm-windows-cpus':'vm-cpus').value,
    memory_gib:el(el('vm-mode').value==='virtualbox'?'vm-windows-memory':'vm-memory').value,
    disk_gib:el(el('vm-mode').value==='virtualbox'?'vm-windows-disk':'vm-disk').value};
}
async function vmAction(path,data){
  vmBusy=true;renderVM();result('vm-result','Working…');
  try{
    const response=await request('/api/config/vm/'+path,data);vmState=response.vm;
    if(settings)settings.vm=vmState;
    if(path==='save'){fillVM(vmState.profile);el('vm-key').value='';}
    renderVM();
    if(!vmState.job?.message)result('vm-result',path==='save'?'Connection saved. Test it or prepare the guest bridge.':vmState.message,true);
  }catch(error){result('vm-result',error.message,false);}
  finally{vmBusy=false;renderVM();}
}
el('vm-form').addEventListener('input',()=>{vmDirty=true;vmMode();renderVM();});
el('vm-form').onsubmit=event=>{event.preventDefault();vmAction('save',{profile:vmProfile(),key:el('vm-key').value});};
el('vm-enabled').onchange=event=>vmAction('enabled',{enabled:event.target.checked});
for(const action of ['test','install','create','connect','disconnect'])el('vm-'+action).onclick=()=>vmAction('action',{action});
el('vm-open').onclick=()=>window.open('/vm?endpoint='+encodeURIComponent(vmState.endpoint),'echopath-vm-viewer','popup=yes,width=1200,height=800,resizable=yes,scrollbars=yes');
el('vm-window').onclick=()=>vmAction('action',{action:'open-window'});
setInterval(async()=>{
  if(vmBusy)return;
  try{const state=await request('/api/config/vm');if(!vmBusy){vmState=state;if(settings)settings.vm=state;renderVM();}}
  catch(error){/* Preserve the form while the local app reconnects. */}
},3000);
