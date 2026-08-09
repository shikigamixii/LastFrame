// Delete modal
let _delCb=null;
function openDeleteModal(msg,cb){$("#deleteModalBody").textContent=msg;_delCb=cb;$("#deleteModal").classList.add("active");}
function closeDeleteModal(){$("#deleteModal").classList.remove("active");_delCb=null;}
$("#deleteModalConfirm").addEventListener("click",async()=>{if(!_delCb)return;const b=$("#deleteModalConfirm");b.textContent="Deleting...";b.disabled=true;try{await _delCb();}finally{b.textContent="Delete";b.disabled=false;closeDeleteModal();}});

// Bulk assign modal
let _bulkCallback=null;
function openBulkAssignModal(items, callback){
  S.bulkAssignItems=items;
  _bulkCallback=callback;
  document.getElementById("bulkAssignCount").innerText=`Assign users to ${items.length} selected item${items.length!==1?'s':''}:`;
  const container=document.getElementById("bulkAssignUsers");
  let h='';
  for(const u of S.users){
    h+=`<div class="assign-chip" data-uid="${u.id}" onclick="this.classList.toggle('on')">${esc(u.name)}</div>`;
  }
  container.innerHTML=h;
  document.getElementById("bulkAssignError").innerText="";
  $("#bulkAssignModal").classList.add("active");
}
function closeBulkAssignModal(){$("#bulkAssignModal").classList.remove("active");_bulkCallback=null;S.bulkAssignItems=[];}
async function confirmBulkAssign(){
  const chips=document.querySelectorAll("#bulkAssignUsers .assign-chip");
  const selectedUsers=[];
  chips.forEach(c=>{if(c.classList.contains("on"))selectedUsers.push(c.dataset.uid);});
  if(selectedUsers.length===0){
    document.getElementById("bulkAssignError").innerText="Please select at least one user.";
    return;
  }
  const btn=document.querySelector("#bulkAssignModal .btn-confirm-delete");
  btn.textContent="Assigning...";
  btn.disabled=true;
  try{
    const resp=await api("/api/assignments/bulk",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({item_ids:S.bulkAssignItems,user_ids:selectedUsers})
    });
    if(resp.success){
      alert(`Assigned ${selectedUsers.length} user(s) to ${resp.assigned_count} items.`);
      const cb=_bulkCallback;
      closeBulkAssignModal();
      S.selectedItemIds.clear();
      if(typeof invalidateWatchSummaryCache==='function')invalidateWatchSummaryCache();
      if(cb)cb();else route();
    }else{
      document.getElementById("bulkAssignError").innerText=resp.error||"Assignment failed";
    }
  }catch(e){
    document.getElementById("bulkAssignError").innerText="Network error";
  }finally{
    btn.textContent="Assign";
    btn.disabled=false;
  }
}

// Settings
async function openSettings(){
  $("#settingsModal").classList.add("active");
  const[libs,cfg,wh,allAccs,provs]=await Promise.all([api("/api/libraries"),api("/api/config"),api("/api/webhook-events/status"),api("/api/accounts"),api("/api/providers")]);
  S.config=cfg;
  // Providers section — enable/disable each media server independently.
  let h='<h3>Media Servers</h3>';
  h+='<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.6rem">Turn a server off to stop querying it and hide its libraries and users. Set or update each server\'s connection details below. Credentials supplied via environment variables take precedence and can\'t be overridden here.</div>';
  // Per-provider credential editors. Field names match the keys accepted by
  // POST /api/config, which only overwrites a value when a non-empty one is
  // sent — so leaving the secret box blank keeps the stored credential.
  const provCredFields={
    plex:{url:"plex_url",secret:"plex_token",secretLabel:"Plex Token",urlPh:"Plex Server URL (e.g. https://xxx.plex.direct:32400)"},
    jellyfin:{url:"jellyfin_url",secret:"jellyfin_api_key",secretLabel:"API Key",urlPh:"Jellyfin Server URL (e.g. https://jellyfin.example.com)"}
  };
  const credInputStyle="width:100%;padding:0.4rem 0.5rem;background:var(--bg-primary);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);font-size:0.82rem;box-sizing:border-box";
  for(const p of provs){
    h+='<div class="lib-toggle"><div><div class="lib-toggle-name">'+esc(p.label)+'</div><div class="lib-toggle-type">'+(p.configured?'':'credentials not set')+'</div></div>';
    h+='<div class="toggle-switch prov-toggle '+(p.enabled?"on":"")+'" data-prov-key="'+esc(p.key)+'" '+(p.configured?'onclick="this.classList.toggle(\'on\')"':'style="opacity:0.4;pointer-events:none"')+'></div></div>';
    const cf=provCredFields[p.key];
    if(cf){
      // The secret box is never pre-filled with the stored token — the admin
      // types a new value only when they want to change it.
      h+='<div style="margin:0.4rem 0 1rem">';
      h+='<input class="prov-cred" data-cfg-field="'+esc(cf.url)+'" type="text" value="'+escAttr(cfg[cf.url]||"")+'" placeholder="'+escAttr(cf.urlPh)+'" style="'+credInputStyle+';margin-bottom:0.4rem">';
      h+='<input class="prov-cred" data-cfg-field="'+esc(cf.secret)+'" type="password" autocomplete="new-password" placeholder="'+escAttr(cf.secretLabel+(p.configured?" — leave blank to keep current":""))+'" style="'+credInputStyle+'">';
      if(!p.configured)h+='<div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.3rem">Enter credentials and Save, then re-open Settings to enable this server.</div>';
      h+='</div>';
    }
  }
  h+='<div style="margin-top:1.5rem;border-top:1px solid var(--border);padding-top:1rem"></div>';
  h+='<h3>Select Libraries to Monitor</h3>';
  for(const l of libs){const on=cfg.show_all_libraries||(cfg.monitored_libraries||[]).includes(l.id);h+='<div class="lib-toggle"><div><div class="lib-toggle-name">'+esc(l.name)+'</div><div class="lib-toggle-type">'+(l.type==="tvshows"?"TV Shows":"Movies")+'</div></div><div class="toggle-switch '+(on?"on":"")+'" data-lib-id="'+l.id+'" onclick="this.classList.toggle(\'on\')"></div></div>';}
  // Users section
  h+='<div style="margin-top:1.5rem;border-top:1px solid var(--border);padding-top:1rem"><h3 style="margin-bottom:0.5rem">Manage Users</h3>';
  h+='<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.6rem">Hidden users are excluded from all watch tracking and assignments.</div>';
  for(const a of allAccs){
    h+='<div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.3rem">';
    h+='<span style="flex:1;font-size:0.85rem;'+(a.hidden?"opacity:0.4;text-decoration:line-through":"")+'">'+esc(a.name)+'</span>';
    if(a.hidden){h+="<button class=\"btn-settings\" style=\"padding:0.2rem 0.6rem;font-size:0.75rem\" onclick=\"unhideAccount('"+esc(a.id)+"')\">Show</button>";}
    else{h+="<button class=\"btn-cancel\" style=\"padding:0.2rem 0.6rem;font-size:0.75rem\" onclick=\"hideAccount('"+esc(a.id)+"')\">Hide</button>";}
    h+='</div>';
  }
  h+='</div>';
  // Webhook section — one URL per enabled provider.
  h+='<div style="margin-top:1.5rem;border-top:1px solid var(--border);padding-top:1rem"><h3 style="margin-bottom:0.5rem">Webhooks</h3>';
  const whHints={plex:"Add in Plex → Settings → Webhooks (requires Plex Pass):",jellyfin:"Add in the Jellyfin Webhook Plugin (Dashboard → Webhooks):"};
  let whIdx=0;
  for(const p of provs.filter(p=>p.enabled)){
    const whUrl=window.location.origin+p.webhookPath;
    h+='<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.4rem"><strong>'+esc(p.label)+'</strong> — '+whHints[p.key]+'</div>';
    h+='<div style="display:flex;gap:0.4rem;margin-bottom:0.75rem"><input id="webhookUrlIn'+whIdx+'" readonly value="'+escAttr(whUrl)+'" style="flex:1;padding:0.4rem 0.5rem;background:var(--bg-primary);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);font-size:0.78rem;font-family:monospace"><button class="btn-settings" style="padding:0.35rem 0.7rem;font-size:0.8rem" onclick="copyWebhookUrl(\'webhookUrlIn'+whIdx+'\')">Copy</button></div>';
    whIdx++;
  }
  h+='<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.5rem">Play events received: <strong>'+wh.play_events+'</strong>';
  if(wh.last_event_at)h+=' &nbsp;|&nbsp; Last event: <strong>'+esc(fmtTs(wh.last_event_at))+'</strong>';
  h+='</div>';
  if(wh.play_events>0){
    const eventsByAcc={};(wh.accounts||[]).forEach(a=>{eventsByAcc[a.account_id]=a.play_events;});
    for(const a of allAccs.filter(a=>!a.hidden)){const cnt=eventsByAcc[a.id]||0;if(cnt)h+='<div style="font-size:0.8rem;margin-bottom:0.2rem"><strong>'+esc(a.name)+'</strong>: '+cnt+' events</div>';}
    h+='<button class="btn-cancel" style="margin-top:0.5rem;padding:0.3rem 0.7rem;font-size:0.78rem" onclick="clearWebhookEvents()">Clear all webhook events</button>';
  }
  h+='</div>';
  // Import Watch History section
  h+='<div style="margin-top:1.5rem;border-top:1px solid var(--border);padding-top:1rem"><h3 style="margin-bottom:0.5rem">Import Watch History</h3>';
  h+='<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.75rem">Import past watch events from each enabled server\'s play history for all users. Safe to run multiple times — existing records are not overwritten.</div>';
  h+='<button class="btn-settings" id="backfillBtn" onclick="importWatchHistory()">Import Watch History</button>';
  h+='<div id="backfillStatus" style="margin-top:0.5rem;font-size:0.8rem;color:var(--text-muted)"></div>';
  h+='</div>';
  // Auto-Delete section
  const adEnabled=!!(cfg.auto_delete_enabled);
  const adLibs=cfg.auto_delete_libraries||[];
  const adGrace=cfg.auto_delete_grace_days!=null?cfg.auto_delete_grace_days:1;
  const adMinDelay=cfg.auto_delete_min_delay_minutes!=null?cfg.auto_delete_min_delay_minutes:30;
  const adDis=adEnabled?'':'disabled style="opacity:0.45;pointer-events:none"';
  h+='<div style="margin-top:1.5rem;border-top:1px solid var(--border);padding-top:1rem"><h3 style="margin-bottom:0.25rem">Auto-Delete</h3>';
  h+='<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.75rem">Automatically delete media once every assigned user has watched it. Enable per-library below. You can also test it on a single TV show from that show\'s detail page.</div>';
  h+='<label style="display:flex;align-items:center;gap:0.5rem;cursor:pointer;margin-bottom:0.75rem"><div class="toggle-switch '+(adEnabled?'on':'')+'" id="adMasterToggle" onclick="toggleAdMaster(this)"></div><span style="font-size:0.88rem;font-weight:500">Enable auto-delete</span></label>';
  h+='<div id="adSubSection" '+(adEnabled?'':'style="opacity:0.45;pointer-events:none"')+'>';
  h+='<div style="display:flex;align-items:center;gap:0.6rem;margin-bottom:0.75rem"><label style="font-size:0.85rem;white-space:nowrap">Grace period (days):</label><input id="adGraceDays" type="number" min="0" max="30" value="'+adGrace+'" style="width:60px;padding:0.25rem 0.4rem;background:var(--bg-primary);border:1px solid var(--border);border-radius:5px;color:var(--text-primary);font-size:0.85rem"><span style="font-size:0.78rem;color:var(--text-muted)">(0 = delete immediately when fully watched)</span></div>';
  h+='<div style="display:flex;align-items:center;gap:0.6rem;margin-bottom:0.75rem"><label style="font-size:0.85rem;white-space:nowrap">Min. delay after completion (min):</label><input id="adMinDelay" type="number" min="0" max="1440" value="'+adMinDelay+'" style="width:60px;padding:0.25rem 0.4rem;background:var(--bg-primary);border:1px solid var(--border);border-radius:5px;color:var(--text-primary);font-size:0.85rem"><span style="font-size:0.78rem;color:var(--text-muted)">(safety buffer after Jellyfin reports playback complete, default 30)</span></div>';
  h+='<div style="font-size:0.82rem;font-weight:500;margin-bottom:0.4rem">Libraries to auto-delete from:</div>';
  for(const l of libs){const on=adLibs.includes(l.id)||adLibs.includes(String(l.id));h+='<div class="lib-toggle"><div><div class="lib-toggle-name">'+esc(l.name)+'</div><div class="lib-toggle-type">'+(l.type==="tvshows"?"TV Shows":"Movies")+'</div></div><div class="toggle-switch ad-lib-toggle '+(on?"on":"")+'" data-lib-id="'+l.id+'" onclick="this.classList.toggle(\'on\')"></div></div>';}
  h+='</div></div>';
  const raWindow=cfg.recently_added_window_days!=null?cfg.recently_added_window_days:30;
  h+='<div style="margin-top:1.5rem;border-top:1px solid var(--border);padding-top:1rem"><h3 style="margin-bottom:0.5rem">Recently Added</h3>';
  h+='<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.5rem">How many days a newly added movie or TV series stays on the Recently Added list before it ages off. Assigning users to a title also removes it.</div>';
  h+='<div style="display:flex;align-items:center;gap:0.6rem"><label style="font-size:0.85rem;white-space:nowrap">Window (days):</label><input id="raWindowDays" type="number" min="1" max="365" value="'+raWindow+'" style="width:70px;padding:0.25rem 0.4rem;background:var(--bg-primary);border:1px solid var(--border);border-radius:5px;color:var(--text-primary);font-size:0.85rem"></div></div>';
  const cfgTz=cfg.timezone||'';
  h+='<div style="margin-top:1.5rem;border-top:1px solid var(--border);padding-top:1rem"><h3 style="margin-bottom:0.5rem">Display Timezone</h3>';
  h+='<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.5rem">IANA timezone for displaying timestamps (e.g. <code>America/New_York</code>, <code>Europe/London</code>). Leave blank to use browser timezone.</div>';
  h+='<input id="cfgTimezone" type="text" value="'+escAttr(cfgTz)+'" placeholder="e.g. America/Los_Angeles" style="width:100%;padding:0.4rem 0.5rem;background:var(--bg-primary);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);font-size:0.85rem;font-family:monospace"></div>';
  $("#settingsBody").innerHTML=h;
}
function toggleAdMaster(el){
  el.classList.toggle('on');
  const sub=document.getElementById('adSubSection');
  if(sub)sub.style.cssText=el.classList.contains('on')?'':'opacity:0.45;pointer-events:none';
}
async function importWatchHistory(){
  const btn=document.getElementById("backfillBtn");
  const status=document.getElementById("backfillStatus");
  if(!btn||!status)return;
  btn.disabled=true;btn.textContent="Importing…";
  status.style.color="var(--text-muted)";status.textContent="Fetching history from your servers, this may take a minute…";
  try{
    const res=await api("/api/admin/backfill-history",{method:"POST"});
    status.style.color="var(--green,#4ade80)";
    status.textContent=`Done — ${res.imported} events imported from ${res.total_history} history entries.`;
  }catch(e){
    status.style.color="var(--red,#f87171)";
    status.textContent="Import failed: "+esc(e.message);
  }finally{
    btn.disabled=false;btn.textContent="Import Watch History";
  }
}
async function hideAccount(id){await api("/api/accounts/"+encodeURIComponent(id)+"/hide",{method:"POST"});S.users=await api("/api/users");openSettings();}
async function unhideAccount(id){await api("/api/accounts/"+encodeURIComponent(id)+"/hide",{method:"DELETE"});S.users=await api("/api/users");openSettings();}
function copyWebhookUrl(elId){const el=document.getElementById(elId||"webhookUrlIn0");if(el){el.select();navigator.clipboard.writeText(el.value);}}
function closeSettings(){$("#settingsModal").classList.remove("active");}
async function saveSettings(){
  const ts=document.querySelectorAll("#settingsBody .toggle-switch:not(.ad-lib-toggle)"),en=[];
  ts.forEach(t=>{if(t.classList.contains("on")&&t.dataset.libId)en.push(t.dataset.libId);});
  const adToggle=document.getElementById("adMasterToggle");
  const adEnabled=adToggle?adToggle.classList.contains("on"):false;
  const adLibToggles=document.querySelectorAll("#settingsBody .ad-lib-toggle"),adLibs=[];
  adLibToggles.forEach(t=>{if(t.classList.contains("on")&&t.dataset.libId)adLibs.push(t.dataset.libId);});
  const adGraceEl=document.getElementById("adGraceDays");
  const adGrace=adGraceEl?Math.max(0,parseInt(adGraceEl.value)||0):1;
  const adMinDelayEl=document.getElementById("adMinDelay");
  const adMinDelay=adMinDelayEl?Math.max(0,parseInt(adMinDelayEl.value)||0):30;
  const tzEl=document.getElementById("cfgTimezone");
  const tz=tzEl?tzEl.value.trim():"";
  const raWinEl=document.getElementById("raWindowDays");
  const raWin=raWinEl?Math.max(1,parseInt(raWinEl.value)||30):30;
  const cfg={monitored_libraries:en,show_all_libraries:en.length===ts.length,
    auto_delete_enabled:adEnabled,auto_delete_libraries:adLibs,auto_delete_grace_days:adGrace,
    auto_delete_min_delay_minutes:adMinDelay,timezone:tz,recently_added_window_days:raWin};
  // Provider enable/disable toggles.
  document.querySelectorAll("#settingsBody .prov-toggle").forEach(t=>{
    if(t.dataset.provKey)cfg[t.dataset.provKey+"_enabled"]=t.classList.contains("on");
  });
  // Provider credentials (URL / token). Only send fields the admin actually
  // filled in; the backend leaves stored values untouched for empty ones.
  document.querySelectorAll("#settingsBody .prov-cred").forEach(inp=>{
    const f=inp.dataset.cfgField,v=(inp.value||"").trim();
    if(f&&v)cfg[f]=v;
  });
  await api("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(cfg)});
  S.config=cfg;S.libraries=[];closeSettings();nav('/');
}
async function clearWebhookEvents(){if(!confirm("Clear all stored webhook events? Watch state will rely on Jellyfin UserData until new events arrive."))return;await api("/api/webhook-events",{method:"DELETE"});openSettings();}

// Admin modal functions
function openAdminModal(){closeSettings();$("#adminModal").classList.add("active");}
function closeAdminModal(){$("#adminModal").classList.remove("active");}
async function saveAdminCredentials(){
  const currentUser=document.getElementById("adminCurrentUsername").value;
  const currentPass=document.getElementById("adminCurrentPassword").value;
  const newUser=document.getElementById("adminNewUsername").value;
  const newPass=document.getElementById("adminNewPassword").value;
  const confirmPass=document.getElementById("adminConfirmPassword").value;
  const err=document.getElementById("adminModalError");
  err.innerText="";
  try{
    const resp=await fetch("/api/admin/change-credentials",{
      method:"POST",headers:{"Content-Type":"application/json","X-CSRFToken":csrfToken()},
      body:JSON.stringify({current_username:currentUser,current_password:currentPass,new_username:newUser,new_password:newPass,confirm_password:confirmPass})
    });
    const data=await resp.json();
    if(data.success){alert("Credentials updated. You will be logged out.");closeAdminModal();await logout();}
    else err.innerText=data.error;
  }catch(e){err.innerText="Network error";}
}

// Login functions
function showLogin(){document.getElementById("loginOverlay").style.display="flex";document.getElementById("logoutBtn").style.display="none";}
async function hideLogin(){document.getElementById("loginOverlay").style.display="none";document.getElementById("logoutBtn").style.display="";if(!S.libraries.length){try{const[u,l]=await Promise.all([api("/api/users"),api("/api/libraries")]);S.users=u;S.libraries=l;}catch(e){}}renderSidebar();route();}
async function doLogin(){
  const username=document.getElementById("loginUsername").value;
  const password=document.getElementById("loginPassword").value;
  const errDiv=document.getElementById("loginError");
  try{
    const resp=await fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json","X-CSRFToken":csrfToken()},body:JSON.stringify({username,password})});
    if(resp.ok){hideLogin();errDiv.innerText="";}
    else{const data=await resp.json();errDiv.innerText=data.error||"Invalid credentials";}
  }catch(e){errDiv.innerText="Login failed";}
}
async function logout(){await fetch("/api/auth/logout",{method:"POST",headers:{"X-CSRFToken":csrfToken()}});showLogin();document.getElementById("content").innerHTML='<div class="loading">Please log in</div>';}
async function checkAuth(){
  try{
    const data=await fetch("/api/auth/status").then(r=>r.json());
    if(data.needs_setup){location.href='/setup';return false;}
    else if(data.logged_in){hideLogin();return true;}
    else{showLogin();return false;}
  }catch(e){showLogin();return false;}
}

