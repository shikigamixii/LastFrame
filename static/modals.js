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
  const[libs,cfg,wh,allAccs]=await Promise.all([api("/api/libraries"),api("/api/config"),api("/api/webhook-events/status"),api("/api/accounts")]);
  S.config=cfg;
  let h='<h3>Select Libraries to Monitor</h3>';
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
  // Webhook section
  h+='<div style="margin-top:1.5rem;border-top:1px solid var(--border);padding-top:1rem"><h3 style="margin-bottom:0.5rem">Plex Webhook</h3>';
  const whUrl=window.location.origin+"/api/webhook/plex";
  h+='<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.5rem">Add this URL in Plex → Settings → Webhooks (requires Plex Pass):</div>';
  h+='<div style="display:flex;gap:0.4rem;margin-bottom:0.75rem"><input id="webhookUrlIn" readonly value="'+esc(whUrl)+'" style="flex:1;padding:0.4rem 0.5rem;background:var(--bg-primary);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);font-size:0.78rem;font-family:monospace"><button class="btn-settings" style="padding:0.35rem 0.7rem;font-size:0.8rem" onclick="copyWebhookUrl()">Copy</button></div>';
  h+='<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.5rem">Play events received: <strong>'+wh.play_events+'</strong>';
  if(wh.last_event_at)h+=' &nbsp;|&nbsp; Last event: <strong>'+esc(fmtTs(wh.last_event_at))+'</strong>';
  h+='</div>';
  if(wh.play_events>0){
    const eventsByAcc={};(wh.accounts||[]).forEach(a=>{eventsByAcc[a.plex_account_id]=a.play_events;});
    for(const a of allAccs.filter(a=>!a.hidden)){const cnt=eventsByAcc[a.id]||0;if(cnt)h+='<div style="font-size:0.8rem;margin-bottom:0.2rem"><strong>'+esc(a.name)+'</strong>: '+cnt+' events</div>';}
    h+='<button class="btn-cancel" style="margin-top:0.5rem;padding:0.3rem 0.7rem;font-size:0.78rem" onclick="clearWebhookEvents()">Clear all webhook events</button>';
  }
  h+='</div>';
  // Import Watch History section
  h+='<div style="margin-top:1.5rem;border-top:1px solid var(--border);padding-top:1rem"><h3 style="margin-bottom:0.5rem">Import Watch History</h3>';
  h+='<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.75rem">Import past watch events from Plex\'s play history for all accounts. Safe to run multiple times — existing records are not overwritten.</div>';
  h+='<button class="btn-settings" id="backfillBtn" onclick="importPlexHistory()">Import Plex History</button>';
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
  h+='<div style="display:flex;align-items:center;gap:0.6rem;margin-bottom:0.75rem"><label style="font-size:0.85rem;white-space:nowrap">Min. delay after scrobble (min):</label><input id="adMinDelay" type="number" min="0" max="1440" value="'+adMinDelay+'" style="width:60px;padding:0.25rem 0.4rem;background:var(--bg-primary);border:1px solid var(--border);border-radius:5px;color:var(--text-primary);font-size:0.85rem"><span style="font-size:0.78rem;color:var(--text-muted)">(Plex scrobbles at ~90% — set to finish-time buffer, default 30)</span></div>';
  h+='<div style="font-size:0.82rem;font-weight:500;margin-bottom:0.4rem">Libraries to auto-delete from:</div>';
  for(const l of libs){const on=adLibs.includes(l.id)||adLibs.includes(String(l.id));h+='<div class="lib-toggle"><div><div class="lib-toggle-name">'+esc(l.name)+'</div><div class="lib-toggle-type">'+(l.type==="tvshows"?"TV Shows":"Movies")+'</div></div><div class="toggle-switch ad-lib-toggle '+(on?"on":"")+'" data-lib-id="'+l.id+'" onclick="this.classList.toggle(\'on\')"></div></div>';}
  h+='</div></div>';
  const cfgTz=cfg.timezone||'';
  h+='<div style="margin-top:1.5rem;border-top:1px solid var(--border);padding-top:1rem"><h3 style="margin-bottom:0.5rem">Display Timezone</h3>';
  h+='<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.5rem">IANA timezone for displaying timestamps (e.g. <code>America/New_York</code>, <code>Europe/London</code>). Leave blank to use browser timezone.</div>';
  h+='<input id="cfgTimezone" type="text" value="'+esc(cfgTz)+'" placeholder="e.g. America/Los_Angeles" style="width:100%;padding:0.4rem 0.5rem;background:var(--bg-primary);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);font-size:0.85rem;font-family:monospace"></div>';
  $("#settingsBody").innerHTML=h;
}
function toggleAdMaster(el){
  el.classList.toggle('on');
  const sub=document.getElementById('adSubSection');
  if(sub)sub.style.cssText=el.classList.contains('on')?'':'opacity:0.45;pointer-events:none';
}
async function importPlexHistory(){
  const btn=document.getElementById("backfillBtn");
  const status=document.getElementById("backfillStatus");
  if(!btn||!status)return;
  btn.disabled=true;btn.textContent="Importing…";
  status.style.color="var(--text-muted)";status.textContent="Fetching history from Plex, this may take a minute…";
  try{
    const res=await api("/api/admin/backfill-history",{method:"POST"});
    status.style.color="var(--green,#4ade80)";
    status.textContent=`Done — ${res.imported} events imported from ${res.total_history} history entries.`;
  }catch(e){
    status.style.color="var(--red,#f87171)";
    status.textContent="Import failed: "+esc(e.message);
  }finally{
    btn.disabled=false;btn.textContent="Import Plex History";
  }
}
async function hideAccount(id){await api("/api/accounts/"+encodeURIComponent(id)+"/hide",{method:"POST"});S.users=await api("/api/users");openSettings();}
async function unhideAccount(id){await api("/api/accounts/"+encodeURIComponent(id)+"/hide",{method:"DELETE"});S.users=await api("/api/users");openSettings();}
function copyWebhookUrl(){const el=document.getElementById("webhookUrlIn");if(el){el.select();navigator.clipboard.writeText(el.value);}}
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
  const cfg={monitored_libraries:en,show_all_libraries:en.length===ts.length,
    auto_delete_enabled:adEnabled,auto_delete_libraries:adLibs,auto_delete_grace_days:adGrace,
    auto_delete_min_delay_minutes:adMinDelay,timezone:tz};
  await api("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(cfg)});
  S.config=cfg;S.libraries=[];closeSettings();nav('/');
}
async function clearWebhookEvents(){if(!confirm("Clear all stored webhook events? Watch state will fall back to Plex API data until new events arrive."))return;await api("/api/webhook-events",{method:"DELETE"});openSettings();}

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

