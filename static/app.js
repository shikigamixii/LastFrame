const S={users:[],libraries:[],config:{},lib:null,series:null,season:null,items:{},episodes:{},assignedIds:null,selectedItemIds:new Set(),bulkAssignItems:[]};
const $=s=>document.querySelector(s),$el=()=>$("#content"),$bc=()=>$("#breadcrumbs");

async function api(p,o){
  const r=await fetch(p,o);
  if(r.status===401){
    const data=await r.json();
    if(data.login_required) showLogin();
    throw new Error("Unauthorized");
  }
  if(!r.ok)throw new Error("API "+r.status);
  return r.json();
}

function fmtT(t){if(!t)return"";const s=Math.floor(t/1e7),h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h?h+"h "+m+"m":m+"m";}
function fmtD(iso){if(!iso)return"";const d=Math.floor((Date.now()-new Date(iso))/864e5);if(d===0)return"Today";if(d===1)return"Yesterday";if(d<7)return d+"d ago";if(d<30)return Math.floor(d/7)+"w ago";return new Date(iso).toLocaleDateString("en-US",{month:"short",day:"numeric",year:"numeric"});}
function esc(s){const d=document.createElement("div");d.textContent=s;return d.innerHTML;}
function cleanName(s){return s.replace(/\s*\{[^}]+\}/g,'').trim();}
function badge(u){
  if(u.played)return'<div><span class="watch-badge watched">✓ '+esc(u.userName)+'</span><div class="watch-meta">'+u.playCount+'× played'+(u.lastPlayedDate?' · '+fmtD(u.lastPlayedDate):'')+'</div></div>';
  if(u.playedPercentage>0)return'<div><span class="watch-badge partial">◐ '+esc(u.userName)+' '+u.playedPercentage+'%</span><div class="watch-meta">'+u.playCount+'× played'+(u.lastPlayedDate?' · '+fmtD(u.lastPlayedDate):'')+'</div></div>';
  return'<div><span class="watch-badge unwatched">✗ '+esc(u.userName)+'</span></div>';
}
function crumbs(c){$bc().innerHTML=c.map((x,i)=>i===c.length-1?'<span>'+esc(x.label)+'</span>':'<a onclick="nav(\''+x.hash+'\')">'+esc(x.label)+'</a><span class="sep">›</span>').join("");}
function isWatchedByAssigned(epUsers,assignedIds){
  if(!assignedIds)return epUsers.every(u=>u.played);
  const rel=epUsers.filter(u=>assignedIds.includes(u.userId));
  return rel.length>0&&rel.every(u=>u.played);
}

// Routing
function nav(hash){history.pushState(null,'','#'+hash);route();}
async function route(){
  const h=(location.hash||'#/').slice(1),p=h.split('/').filter(Boolean);
  if(!p.length)return viewLibraries();
  if(p[0]==='lib'&&p[1]){
    await ensureLib(p[1]);
    if(p.length===2)return viewGrid(p[1],1);
    if(p[2]==='p')return viewGrid(p[1],parseInt(p[3])||1);
    if(p[2]==='s'&&p[3]){await ensureSeries(p[3]);if(!p[4])return viewSeasons(p[3]);await ensureSeason(p[4],p[3]);return viewEpisodes(p[3],p[4]);}
    if(p[2]==='m'&&p[3]){await ensureItem(p[3]);return viewMovie(p[3]);}
  }
  viewLibraries();
}
window.addEventListener('hashchange',()=>route());
window.addEventListener('popstate',()=>route());
async function ensureLib(id){if(S.lib?.id===id)return;if(!S.libraries.length)S.libraries=await api("/api/libraries");const l=S.libraries.find(x=>x.id===id);if(l){S.lib={id:l.id,name:l.name,type:l.type};return;}const i=await api("/api/item/"+id);S.lib={id,name:i.name,type:i.collectionType==="tvshows"?"tvshows":"movies"};}
async function ensureSeries(id){if(S.series?.id===id)return;const i=await api("/api/item/"+id);S.series={id,name:i.name};S.items[id]={name:i.name,type:"series"};}
async function ensureSeason(id,sid){if(S.season?.id===id)return;const i=await api("/api/item/"+id);S.season={id,name:i.name,seriesId:sid};S.items[id]={name:i.name,type:"season",seriesId:sid};}
async function ensureItem(id){if(S.items[id])return;const i=await api("/api/item/"+id);S.items[id]={name:i.name,type:i.type==="Movie"?"movie":"unknown"};}

// Delete modal
let _delCb=null;
function openDeleteModal(msg,cb){$("#deleteModalBody").innerHTML=msg;_delCb=cb;$("#deleteModal").classList.add("active");}
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
  const[libs,cfg]=await Promise.all([api("/api/libraries"),api("/api/config")]);S.config=cfg;
  let h='<h3>Select Libraries to Monitor</h3>';
  for(const l of libs){const on=cfg.show_all_libraries||(cfg.monitored_libraries||[]).includes(l.id);h+='<div class="lib-toggle"><div><div class="lib-toggle-name">'+esc(l.name)+'</div><div class="lib-toggle-type">'+(l.type==="tvshows"?"TV Shows":"Movies")+'</div></div><div class="toggle-switch '+(on?"on":"")+'" data-lib-id="'+l.id+'" onclick="this.classList.toggle(\'on\')"></div></div>';}
  $("#settingsBody").innerHTML=h;
}
function closeSettings(){$("#settingsModal").classList.remove("active");}
async function saveSettings(){const ts=document.querySelectorAll("#settingsBody .toggle-switch"),en=[];ts.forEach(t=>{if(t.classList.contains("on"))en.push(t.dataset.libId);});const cfg={monitored_libraries:en,show_all_libraries:en.length===ts.length};await api("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(cfg)});S.config=cfg;S.libraries=[];closeSettings();nav('/');}

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
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({current_username:currentUser,current_password:currentPass,new_username:newUser,new_password:newPass,confirm_password:confirmPass})
    });
    const data=await resp.json();
    if(data.success){alert("Credentials updated. You will be logged out.");closeAdminModal();await logout();}
    else err.innerText=data.error;
  }catch(e){err.innerText="Network error";}
}

// Login functions
function showLogin(){document.getElementById("loginOverlay").style.display="flex";document.getElementById("logoutBtn").style.display="none";}
function hideLogin(){document.getElementById("loginOverlay").style.display="none";document.getElementById("logoutBtn").style.display="block";route();}
async function doLogin(){
  const username=document.getElementById("loginUsername").value;
  const password=document.getElementById("loginPassword").value;
  const errDiv=document.getElementById("loginError");
  try{
    const resp=await fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username,password})});
    if(resp.ok){hideLogin();errDiv.innerText="";}
    else{const data=await resp.json();errDiv.innerText=data.error||"Invalid credentials";}
  }catch(e){errDiv.innerText="Login failed";}
}
async function logout(){await fetch("/api/auth/logout",{method:"POST"});showLogin();document.getElementById("content").innerHTML='<div class="loading">Please log in</div>';}
async function checkAuth(){
  try{const data=await fetch("/api/auth/status").then(r=>r.json());if(data.logged_in)hideLogin();else showLogin();}
  catch(e){showLogin();}
}

// Debounce
function debounce(fn,ms){let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms);};}

// View Libraries
async function viewLibraries(){
  const el=$el();el.innerHTML='<div class="loading">Loading</div>';crumbs([{label:"Libraries",hash:"/"}]);
  try{
    const[users,libs,recentMovies,recentEpisodes]=await Promise.all([
      api("/api/users"),api("/api/libraries"),
      api("/api/recent/movies").catch(()=>[]),api("/api/recent/episodes").catch(()=>[])
    ]);
    S.users=users;S.libraries=libs;S.lib=null;S.series=null;S.season=null;S.assignedIds=null;
    const vis=libs.filter(l=>l.monitored);
    let libsHtml=vis.length?'<div class="lib-grid">'+vis.map(l=>'<div class="lib-card" onclick="nav(\'/lib/'+l.id+'\')"><h3>'+esc(l.name)+'</h3><div class="lib-type">'+(l.type==="tvshows"?"📺 TV Shows":"🎬 Movies")+'</div></div>').join("")+'</div>':'<div class="empty-state">No monitored libraries. Click ⚙ Libraries to select.</div>';
    const moviesLib=S.libraries.find(l=>l.type==='movies');const showsLib=S.libraries.find(l=>l.type==='tvshows');

    function renderRow(title, items, type, getPath, getIcon, getTitle, getSubtitle) {
      if(!items.length) return '';
      return `
        <div style="margin:2rem 0 1rem 0;">
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:0.75rem;">
            <h3 style="margin:0">${title}</h3>
            <span style="color:var(--text-muted);font-size:0.8rem;cursor:pointer;" onclick="alert('View all coming soon')">›</span>
          </div>
          <div style="display:flex;overflow-x:auto;gap:1rem;padding-bottom:0.5rem;scrollbar-width:thin;">
            ${items.map(item => `
              <div class="item-card" style="min-width:150px; max-width:200px; flex-shrink:0;" onclick="nav('${getPath(item)}')">
                <div class="poster">
                  <img src="${item.imageUrl}" loading="lazy" onerror="this.parentElement.innerHTML='${getIcon(item)}'">
                </div>
                <div class="info">
                  <h4 title="${esc(getTitle(item))}">${esc(getTitle(item))}</h4>
                  <div class="year">${getSubtitle(item)}</div>
                </div>
              </div>
            `).join('')}
          </div>
        </div>
      `;
    }

    const recentMoviesHtml = renderRow('Recently Watched Movies', recentMovies, 'movie',
      (m) => `/lib/${moviesLib?.id}/m/${m.id}`,
      () => '🎬',
      (m) => m.name,
      (m) => m.year || ''
    );

    const recentShowsHtml = renderRow('Recently Watched Episodes', recentEpisodes, 'episode',
      (e) => `/lib/${showsLib?.id}/s/${e.seriesId}`,
      () => '📺',
      (e) => e.seriesName,
      (e) => `${e.seasonName} • E${e.episodeNumber}`
    );

    el.innerHTML = libsHtml + recentMoviesHtml + recentShowsHtml;
    vis.forEach(l=>{S.items[l.id]={name:l.name,type:l.type};});
  }catch(e){el.innerHTML='<div class="empty-state">Failed to connect.<br><small>'+esc(e.message)+'</small></div>';}
}

// Grid with selection toolbar
let _currentGridItems=[], _currentLibId=null, _currentPage=1, _currentPaginationData=null;
let _activeGenre=null, _showOnlyWatched=false, _watchSummary=null, _watchSummaryLoading=false, _watchSummaryLibId=null;
async function viewGrid(libId,page){
  page=page||1;
  if(_currentLibId!==libId){_activeGenre=null;_showOnlyWatched=false;_watchSummary=null;_watchSummaryLibId=null;_currentPaginationData=null;}
  _currentLibId=libId;_currentPage=page;
  const el=$el();el.innerHTML='<div class="loading">Loading</div>';const lib=S.lib;
  crumbs([{label:"Libraries",hash:"/"},{label:lib.name,hash:"/lib/"+libId}]);
  try{
    const ep=lib.type==="movies"?"/api/movies":"/api/series";
    let url=ep+"?parentId="+libId+"&page="+page;
    if(_activeGenre)url+="&genre="+encodeURIComponent(_activeGenre);
    const data=await api(url);
    _currentGridItems=data.items;_currentPaginationData=data;
    data.items.forEach(s=>{S.items[s.id]={name:s.name,type:lib.type==="movies"?"movie":"series",year:s.year};});
    let h=`<div class="search-bar"><span class="icon">🔍</span><input type="text" id="itemSearch" placeholder="Search entire library..." oninput="onSearch(this.value)"><div class="search-hint">Searches all pages, not just the current one</div></div>`;
    h+=`<div class="filter-bar" id="filterBar">
      <div class="genre-chips" id="genreChips"><span class="genre-chips-loading">Loading genres…</span></div>
      <div class="watched-filter-row">
        <button class="watched-toggle-btn${_showOnlyWatched?' active':''}" id="watchedToggleBtn" onclick="toggleWatchedFilter()" disabled>✓ All Watched Only</button>
        <span class="watched-summary-text" id="watchedSummaryText">Loading…</span>
      </div>
    </div>`;
    h+=`<div class="selection-toolbar">
      <button onclick="toggleSelectAll()">Select All</button>
      <button onclick="gridSelectNone()">Deselect All</button>
      <span id="gridSelCount" style="color:var(--text-muted)">0 selected</span>
      <button class="assign-selected" id="btnAssignSel" disabled onclick="openBulkAssignFromSelection()">Assign Selected</button>
    </div>`;
    h+='<div class="item-grid" id="gridItems"></div>';
    el.innerHTML=h;
    renderGridItems(data.items,libId,lib);
    renderPagination(data,libId,page);
    if(_showOnlyWatched&&_watchSummary)applyWatchedFilter();
    attachSearchListener(libId);
    fetchGenres(libId,lib.type);
    fetchWatchSummary(libId,lib.type);
  }catch(e){el.innerHTML='<div class="empty-state">Error: '+esc(e.message)+'</div>';}
}
function renderGridItems(items,libId,lib){
  const icon=lib.type==="movies"?"🎬":"📺",pfx=lib.type==="movies"?"m":"s";
  const container=document.getElementById("gridItems");
  if(!container)return;
  const displayItems=(_showOnlyWatched&&_watchSummary)?items.filter(s=>_watchSummary[s.id]===true):items;
  let h='';
  for(const s of displayItems){
    const isSelected=S.selectedItemIds.has(s.id);
    const isWatched=_watchSummary&&_watchSummary[s.id]===true;
    h+=`
      <div class="item-card" data-id="${s.id}">
        <div class="checkbox-overlay">
          <input type="checkbox" class="grid-checkbox" data-id="${s.id}" ${isSelected?'checked':''} onclick="event.stopPropagation(); toggleItemSelection('${s.id}', this.checked)">
        </div>
        ${isWatched?`<div class="watched-overlay"><span class="all-watched-tag">All Watched</span></div>`:''}
        <div onclick="nav('/lib/${libId}/${pfx}/${s.id}')">
          <div class="poster"><img src="/api/image/${s.id}?type=Primary&maxWidth=300" loading="lazy" onerror="this.parentElement.innerHTML='${icon}'"></div>
          <div class="info"><h4 title="${esc(cleanName(s.name))}">${esc(cleanName(s.name))}</h4>${s.year?`<div class="year">${s.year}</div>`:''}</div>
        </div>
      </div>
    `;
  }
  if(!displayItems.length&&_showOnlyWatched)h='<div class="empty-state" style="grid-column:1/-1">No fully watched items on this page.</div>';
  container.innerHTML=h;
  updateGridSelectionCount();
}
function getPageRange(cur,total){
  if(total<=7)return Array.from({length:total},(_,i)=>i+1);
  const s=new Set([1,total]);
  for(let i=Math.max(1,cur-2);i<=Math.min(total,cur+2);i++)s.add(i);
  const arr=Array.from(s).sort((a,b)=>a-b);
  const r=[];
  for(let i=0;i<arr.length;i++){if(i>0&&arr[i]-arr[i-1]>1)r.push('...');r.push(arr[i]);}
  return r;
}
function renderPagination(data,libId,page){
  const container=document.getElementById("gridItems")?.parentNode;
  if(!container)return;
  let pagHtml='';
  if(!data.isSearch){
    const tp=Math.ceil(data.totalCount/data.pageSize);
    if(tp>1){
      let nums='';
      for(const r of getPageRange(page,tp)){
        if(r==='...')nums+=`<span class="page-ellipsis">…</span>`;
        else nums+=`<button class="page-btn${r===page?' current':''}"${r===page?' disabled':''} onclick="nav('/lib/${libId}/p/${r}')">${r}</button>`;
      }
      pagHtml=`<div class="pagination"><button ${page<=1?'disabled':''} onclick="nav('/lib/${libId}/p/1')" title="First">«</button><button ${page<=1?'disabled':''} onclick="nav('/lib/${libId}/p/${page-1}')" title="Previous">‹</button>${nums}<button ${page>=tp?'disabled':''} onclick="nav('/lib/${libId}/p/${page+1}')" title="Next">›</button><button ${page>=tp?'disabled':''} onclick="nav('/lib/${libId}/p/${tp}')" title="Last">»</button><span class="page-info">${data.totalCount} items · Page ${page} of ${tp}</span></div>`;
    }
  }else{
    pagHtml=`<div class="pagination"><span class="page-info">${data.items.length} results</span></div>`;
  }
  const existing=container.querySelector('.pagination');
  if(existing)existing.remove();
  container.insertAdjacentHTML('beforeend',pagHtml);
}
function attachSearchListener(libId){
  const input=document.getElementById("itemSearch");
  if(!input)return;
  const searchFn=debounce(async(q)=>{
    const ep=S.lib.type==="movies"?"/api/movies":"/api/series";
    let url=ep+"?parentId="+libId+(q?"&search="+encodeURIComponent(q):"");
    if(_activeGenre)url+="&genre="+encodeURIComponent(_activeGenre);
    const data=await api(url);
    renderGridItems(data.items,libId,S.lib);
    renderPagination(data,libId,1);
  },300);
  input.oninput=()=>searchFn(input.value);
}
function toggleItemSelection(id,checked){
  if(checked) S.selectedItemIds.add(id);
  else S.selectedItemIds.delete(id);
  updateGridSelectionCount();
  const btn=document.getElementById("btnAssignSel");
  if(btn) btn.disabled=S.selectedItemIds.size===0;
}
function toggleSelectAll(){
  const checkboxes=document.querySelectorAll(".grid-checkbox");
  const allChecked=Array.from(checkboxes).every(cb=>cb.checked);
  checkboxes.forEach(cb=>{
    cb.checked=!allChecked;
    const id=cb.dataset.id;
    if(cb.checked) S.selectedItemIds.add(id);
    else S.selectedItemIds.delete(id);
  });
  updateGridSelectionCount();
  const btn=document.getElementById("btnAssignSel");
  if(btn) btn.disabled=S.selectedItemIds.size===0;
}
function gridSelectNone(){
  document.querySelectorAll(".grid-checkbox").forEach(cb=>{
    cb.checked=false;
    S.selectedItemIds.delete(cb.dataset.id);
  });
  updateGridSelectionCount();
  const btn=document.getElementById("btnAssignSel");
  if(btn) btn.disabled=true;
}
function updateGridSelectionCount(){
  const span=document.getElementById("gridSelCount");
  if(span) span.textContent=`${S.selectedItemIds.size} selected`;
}
function openBulkAssignFromSelection(){
  if(S.selectedItemIds.size===0)return;
  const items=Array.from(S.selectedItemIds);
  openBulkAssignModal(items,()=>{
    S.selectedItemIds.clear();
    updateGridSelectionCount();
    const btn=document.getElementById("btnAssignSel");
    if(btn) btn.disabled=true;
    route();
  });
}
function onSearch(q){
  const input=document.getElementById("itemSearch");
  if(input && input.value!==q) return;
  const event=new Event('input');
  document.getElementById("itemSearch")?.dispatchEvent(event);
}

// Watch summary filter
async function applyWatchedFilter(){
  if(!_watchSummary)return;
  const watchedIds=Object.entries(_watchSummary).filter(([,v])=>v===true).map(([k])=>k);
  const pagCont=document.getElementById("gridItems")?.parentNode;
  if(pagCont){const ex=pagCont.querySelector('.pagination');if(ex)ex.remove();}
  if(!watchedIds.length){
    const c=document.getElementById("gridItems");
    if(c)c.innerHTML='<div class="empty-state" style="grid-column:1/-1">No fully watched items.</div>';
    return;
  }
  try{
    const ep=S.lib.type==="movies"?"/api/movies":"/api/series";
    let url=ep+"?parentId="+_currentLibId+"&ids="+encodeURIComponent(watchedIds.join(","));
    if(_activeGenre)url+="&genre="+encodeURIComponent(_activeGenre);
    const data=await api(url);
    if(_currentLibId!==_currentLibId)return;
    renderGridItems(data.items,_currentLibId,S.lib);
    const cont=document.getElementById("gridItems")?.parentNode;
    if(cont){
      const ex=cont.querySelector('.pagination');if(ex)ex.remove();
      const label=_activeGenre?`${data.items.length} fully watched in ${esc(_activeGenre)}`:`${watchedIds.length} fully watched`;
      cont.insertAdjacentHTML('beforeend',`<div class="pagination"><span class="page-info">${label}</span></div>`);
    }
  }catch(e){renderGridItems(_currentGridItems,_currentLibId,S.lib);}
}
async function fetchWatchSummary(libId,libType){
  if(_watchSummaryLibId===libId&&_watchSummary!==null){updateWatchedFilterUI();if(_showOnlyWatched)applyWatchedFilter();else renderGridItems(_currentGridItems,libId,S.lib);return;}
  if(_watchSummaryLoading)return;
  _watchSummaryLoading=true;
  try{
    const type=libType==="movies"?"movies":"series";
    const data=await api(`/api/watch-summary?parentId=${libId}&type=${type}`);
    _watchSummary=data;_watchSummaryLibId=libId;
    updateWatchedFilterUI();
    if(_currentLibId===libId){if(_showOnlyWatched)applyWatchedFilter();else renderGridItems(_currentGridItems,libId,S.lib);}
  }catch(e){
    const txt=document.getElementById("watchedSummaryText");
    if(txt)txt.textContent="Watch data unavailable";
  }finally{_watchSummaryLoading=false;}
}
function updateWatchedFilterUI(){
  if(!_watchSummary)return;
  const watchedCount=Object.values(_watchSummary).filter(v=>v===true).length;
  const total=Object.keys(_watchSummary).length;
  const btn=document.getElementById("watchedToggleBtn");
  const txt=document.getElementById("watchedSummaryText");
  if(btn){btn.disabled=false;btn.className="watched-toggle-btn"+(_showOnlyWatched?" active":"");}
  if(txt)txt.textContent=`${watchedCount} of ${total} fully watched`;
}
async function toggleWatchedFilter(){
  if(!_watchSummary)return;
  _showOnlyWatched=!_showOnlyWatched;
  const btn=document.getElementById("watchedToggleBtn");
  if(btn){btn.className="watched-toggle-btn"+(_showOnlyWatched?" active":"");btn.disabled=true;}
  if(_showOnlyWatched){
    await applyWatchedFilter();
  }else{
    renderGridItems(_currentGridItems,_currentLibId,S.lib);
    if(_currentPaginationData)renderPagination(_currentPaginationData,_currentLibId,_currentPage);
  }
  if(btn)btn.disabled=false;
}

// Genre filter
async function fetchGenres(libId,libType){
  const container=document.getElementById("genreChips");
  if(!container)return;
  try{
    const type=libType==="movies"?"movies":"series";
    const genres=await api(`/api/genres?parentId=${libId}&type=${type}`);
    renderGenreChips(genres,libId);
  }catch(e){
    if(container)container.innerHTML='<span style="color:var(--text-muted);font-size:.8rem">Genres unavailable</span>';
  }
}
function renderGenreChips(genres,libId){
  const container=document.getElementById("genreChips");
  if(!container)return;
  if(!genres||!genres.length){container.innerHTML='';return;}
  let h=`<span class="genre-chip all-chip${!_activeGenre?' active':''}" data-genre="" data-libid="${libId}">All</span>`;
  for(const g of genres){
    h+=`<span class="genre-chip${_activeGenre===g?' active':''}" data-genre="${esc(g)}" data-libid="${libId}">${esc(g)}</span>`;
  }
  container.innerHTML=h;
  container.querySelectorAll('.genre-chip').forEach(el=>{
    el.addEventListener('click',()=>setGenreFilter(el.dataset.genre||null,el.dataset.libid));
  });
}
function setGenreFilter(genre,libId){
  if(_activeGenre===genre)return;
  _activeGenre=genre;
  nav('/lib/'+libId);
}

// Assignment panel (single item)
function renderAssignPanel(itemId,assignData,isSeries=true){
  const mode=assignData.mode,assigned=new Set(assignData.assigned);
  let h='<div class="assign-panel"><h3>Assigned Users</h3><div class="assign-desc">Only assigned users count toward "watched by all." Default: everyone.</div>';
  if(isSeries)h+='<div class="assign-scope">✓ Applies to all seasons and future episodes of this series.</div>';
  h+='<div class="assign-users" id="assignUsers">';
  for(const u of S.users){const on=mode==="all"||assigned.has(u.id);h+='<div class="assign-chip '+(on?"on":"")+'" data-uid="'+u.id+'" onclick="this.classList.toggle(\'on\')">'+esc(u.name)+'</div>';}
  h+='</div><div class="assign-actions"><button class="btn-save" onclick="saveAssign(\''+itemId+'\')">Save</button><button onclick="resetAssign(\''+itemId+'\')">Reset to All</button><span class="assign-status" id="assignStatus">Saved!</span></div></div>';
  return h;
}
async function saveAssign(itemId){
  const chips=document.querySelectorAll("#assignUsers .assign-chip");
  const on=[];chips.forEach(c=>{if(c.classList.contains("on"))on.push(c.dataset.uid);});
  if(on.length===S.users.length){await api("/api/assignments/"+itemId,{method:"DELETE"});S.assignedIds=null;}
  else{await api("/api/assignments/"+itemId,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({userIds:on})});S.assignedIds=on;}
  const st=$("#assignStatus");if(st){st.classList.add("show");setTimeout(()=>st.classList.remove("show"),2000);}
  route();
}
async function resetAssign(itemId){await api("/api/assignments/"+itemId,{method:"DELETE"});S.assignedIds=null;const st=$("#assignStatus");if(st){st.classList.add("show");setTimeout(()=>st.classList.remove("show"),2000);}route();}

// Movie detail
async function viewMovie(movieId){
  const el=$el();el.innerHTML='<div class="loading">Loading</div>';const lib=S.lib,it=S.items[movieId]||{name:"Movie"};
  crumbs([{label:"Libraries",hash:"/"},{label:lib.name,hash:"/lib/"+lib.id},{label:it.name}]);
  try{const[ws,assign]=await Promise.all([api("/api/watch-status/"+movieId),api("/api/assignments/"+movieId)]);
    S.assignedIds=assign.mode==="custom"?assign.assigned:null;
    let displayUsers=ws;if(assign.mode==="custom"&&assign.assigned.length){const assignedSet=new Set(assign.assigned);displayUsers=ws.filter(u=>assignedSet.has(u.userId));}
    const allW=isWatchedByAssigned(ws,S.assignedIds);
    let h=renderAssignPanel(movieId,assign,false);
    h+='<div class="movie-detail"><div class="movie-detail-header"><div class="movie-detail-poster"><img src="/api/image/'+movieId+'?type=Primary&maxWidth=400" onerror="this.parentElement.innerHTML=\'🎬\'" alt=""></div><div class="movie-detail-info"><h2>'+esc(it.name)+(allW?'<span class="all-watched-tag">All Watched</span>':'')+'</h2>'+(it.year?'<div class="year">'+it.year+'</div>':'')+'<div style="margin-top:1rem"><button class="btn-delete-bulk" onclick="deleteMovie(\''+movieId+'\')">Delete Movie</button></div></div></div><div class="movie-watch-list">';
    for(const u of displayUsers)h+='<div class="movie-watch-item">'+badge(u)+'</div>';
    h+='</div></div>';el.innerHTML=h;
  }catch(e){el.innerHTML='<div class="empty-state">Error: '+esc(e.message)+'</div>';}
}

// Seasons with poster & progress bars
async function viewSeasons(seriesId){
  const el=$el();el.innerHTML='<div class="loading">Loading</div>';const lib=S.lib,ser=S.series;S.season=null;
  crumbs([{label:"Libraries",hash:"/"},{label:lib.name,hash:"/lib/"+lib.id},{label:cleanName(ser.name)}]);
  try{const[seasons,assign]=await Promise.all([api("/api/seasons/"+seriesId),api("/api/assignments/"+seriesId)]);
    S.assignedIds=assign.mode==="custom"?assign.assigned:null;
    if(!seasons.length){el.innerHTML='<div class="empty-state">No seasons found.</div>';return;}
    seasons.forEach(s=>{S.items[s.id]={name:s.name,type:"season",seriesId};});
    let posterHtml=`<div style="display:flex; gap:1.5rem; margin-bottom:1.5rem; align-items:center;"><div style="width:120px; border-radius:8px; overflow:hidden; background:var(--bg-card); aspect-ratio:2/3; flex-shrink:0;"><img src="/api/image/${seriesId}?type=Primary&maxWidth=200" onerror="this.parentElement.innerHTML='📺'" style="width:100%; height:100%; object-fit:cover;"></div><div><h2 style="margin-bottom:0.25rem">${esc(cleanName(ser.name))}</h2><div class="lib-type">Series</div></div></div>`;
    let h=renderAssignPanel(seriesId,assign,true)+posterHtml+'<div class="season-list">';
    for(const s of seasons){
      const totalEp=s.totalEpisodes||0,completedUsers=s.completedUsers||0,totalUsers=s.totalAssignedUsers||S.users.length,percent=totalUsers?(completedUsers/totalUsers)*100:0;
      let tooltipLines=[];if(s.userProgress&&s.userProgress.length){for(const up of s.userProgress){tooltipLines.push(`${up.userName}: ${up.playedCount}/${up.totalCount} ${up.completed?'✓':''}`);}}
      const tooltipText=tooltipLines.join('\n');
      h+=`<div class="season-item"><div style="display:flex;justify-content:space-between;align-items:center;"><span class="season-item-name" onclick="nav('/lib/${lib.id}/s/${seriesId}/${s.id}')">${esc(s.name)}</span><button class="btn-delete" onclick="deleteSeason('${s.id}','${seriesId}')">Delete</button></div><div style="margin-top:8px;" title="${esc(tooltipText)}"><div style="display:flex;justify-content:space-between;font-size:0.75rem;color:var(--text-muted);margin-bottom:4px;"><span>${completedUsers}/${totalUsers} users completed</span><span>${Math.round(percent)}%</span></div><div style="background:var(--border);border-radius:4px;height:6px;overflow:hidden;"><div style="width:${percent}%;background:var(--green);height:100%;border-radius:4px;"></div></div></div></div>`;
    }
    h+='</div>';el.innerHTML=h;
  }catch(e){el.innerHTML='<div class="empty-state">Error: '+esc(e.message)+'</div>';}
}

// Episodes
async function viewEpisodes(seriesId,seasonId){
  const el=$el();el.innerHTML='<div class="loading">Loading watch data</div>';const lib=S.lib,ser=S.series,sea=S.season;
  crumbs([{label:"Libraries",hash:"/"},{label:lib.name,hash:"/lib/"+lib.id},{label:cleanName(ser.name),hash:"/lib/"+lib.id+"/s/"+seriesId},{label:cleanName(sea.name)}]);
  try{const[episodes,assign]=await Promise.all([api("/api/season-watch-status/"+seriesId+"/"+seasonId),api("/api/assignments/"+seriesId)]);
    S.assignedIds=assign.mode==="custom"?assign.assigned:null;
    if(!episodes.length){try{await api("/api/delete/"+seasonId,{method:"DELETE"});}catch(e){}nav("/lib/"+lib.id+"/s/"+seriesId);return;}
    S.episodes={};episodes.forEach(ep=>{S.episodes[ep.id]={name:ep.name,index:ep.indexNumber,allWatched:isWatchedByAssigned(ep.users,S.assignedIds)};});
    let uNames=[];if(assign.mode==="custom"&&assign.assigned.length){const assignedSet=new Set(assign.assigned);if(episodes[0]?.users){uNames=episodes[0].users.filter(u=>assignedSet.has(u.userId)).map(u=>u.userName);}}else{if(episodes[0]?.users)uNames=episodes[0].users.map(u=>u.userName);}
    const total=episodes.length,watchedAll=Object.values(S.episodes).filter(e=>e.allWatched).length;
    let posterHtml=`<div style="display:flex; gap:1.5rem; margin-bottom:1.5rem; align-items:center;"><div style="width:120px; border-radius:8px; overflow:hidden; background:var(--bg-card); aspect-ratio:2/3; flex-shrink:0;"><img src="/api/image/${seasonId}?type=Primary&maxWidth=200" onerror="this.parentElement.innerHTML='📺'" style="width:100%; height:100%; object-fit:cover;"></div><div><h2 style="margin-bottom:0.25rem">${esc(cleanName(sea.name))}</h2><div class="lib-type">${esc(cleanName(ser.name))}</div></div></div>`;
    let h=posterHtml+'<div class="watch-summary"><div><strong>'+watchedAll+'</strong> of <strong>'+total+'</strong> watched by '+(S.assignedIds?'assigned users':'all users')+(watchedAll===total?' — <span style="color:var(--green)">safe to delete</span>':'')+'</div>'+(watchedAll>0?'<button class="btn-delete-bulk" onclick="deleteAllWatched()">Delete '+watchedAll+' watched</button>':'')+'</div>';
    h+='<div class="selection-toolbar"><button onclick="selectAll()">Select All</button><button onclick="selectWatched()">Select Watched</button><button onclick="selectNone()">Deselect All</button><span id="selCount" style="color:var(--text-muted)">0 selected</span><button class="delete-selected" id="btnDelSel" disabled onclick="deleteSelected()">Delete Selected</button></div>';
    h+='<div class="table-wrap"><table class="episode-table"><thead><tr><th style="width:30px"><input type="checkbox" class="ep-checkbox" onchange="toggleAll(this.checked)"></th><th>Episode</th>'+uNames.map(n=>'<th class="watch-cell">'+esc(n)+'</th>').join('')+'<th>Actions</th></tr></thead><tbody>';
    for(const ep of episodes){const allD=S.episodes[ep.id].allWatched,umap={};ep.users.forEach(u=>{umap[u.userName]=u;});h+='<tr id="ep-'+ep.id+'"><td><input type="checkbox" class="ep-checkbox" data-ep-id="'+ep.id+'" onchange="updateSelCount()"></td><td><span class="ep-num">E'+String(ep.indexNumber).padStart(2,"0")+'</span><span class="ep-name">'+esc(ep.name)+'</span>'+(ep.runTimeTicks?'<span class="ep-runtime">('+fmtT(ep.runTimeTicks)+')</span>':'')+(allD?'<span class="all-watched-tag">All</span>':'')+'</td>';for(const n of uNames){const u=umap[n];h+='<td class="watch-cell">'+(u?badge(u):'<span class="watch-badge unwatched">—</span>')+'</td>';}h+='<td><button class="btn-delete" onclick="deleteEpisode(\''+ep.id+'\')">Delete</button></td>';}
    h+='</tbody></table></div>';el.innerHTML=h;
  }catch(e){el.innerHTML='<div class="empty-state">Error: '+esc(e.message)+'</div>';}
}
// Selection helpers (episodes)
function getCbs(){return document.querySelectorAll('tbody .ep-checkbox');}
function getChecked(){const ids=[];getCbs().forEach(c=>{if(c.checked)ids.push(c.dataset.epId);});return ids;}
function updateSelCount(){const n=getChecked().length;const el=$("#selCount");if(el)el.textContent=n+" selected";const b=$("#btnDelSel");if(b){b.disabled=n===0;b.textContent="Delete Selected ("+n+")";}}
function toggleAll(v){getCbs().forEach(c=>{c.checked=v;});updateSelCount();}
function selectAll(){toggleAll(true);const th=document.querySelector('thead .ep-checkbox');if(th)th.checked=true;}
function selectNone(){toggleAll(false);const th=document.querySelector('thead .ep-checkbox');if(th)th.checked=false;}
function selectWatched(){getCbs().forEach(c=>{const ep=S.episodes[c.dataset.epId];c.checked=ep?ep.allWatched:false;});updateSelCount();}
// Delete functions
async function afterEpDelete(){if(!S.season)return route();const chk=await api("/api/check-season-empty/"+S.season.seriesId+"/"+S.season.id);if(chk.empty){try{await api("/api/delete/"+S.season.id,{method:"DELETE"});}catch(e){}nav("/lib/"+S.lib.id+"/s/"+S.season.seriesId);}else route();}
function deleteEpisode(id){const ep=S.episodes[id]||{name:id,index:0};openDeleteModal("Delete <strong>E"+String(ep.index).padStart(2,"0")+" - "+esc(ep.name)+"</strong>?",async()=>{await api("/api/delete/"+id,{method:"DELETE"});await afterEpDelete();});}
function deleteSelected(){const ids=getChecked();if(!ids.length)return;const names=ids.map(id=>{const ep=S.episodes[id];return ep?"E"+String(ep.index).padStart(2,"0")+" - "+ep.name:id;});const preview=names.length<=5?names.map(n=>"• "+esc(n)).join("<br>"):names.slice(0,5).map(n=>"• "+esc(n)).join("<br>")+"<br>...and "+(names.length-5)+" more";openDeleteModal("Delete <strong>"+ids.length+"</strong> episodes?<br><br>"+preview,async()=>{await api("/api/delete-batch",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({itemIds:ids})});await afterEpDelete();});}
function deleteAllWatched(){if(!S.season)return;const ids=Object.entries(S.episodes).filter(([,e])=>e.allWatched).map(([id])=>id);if(!ids.length)return;openDeleteModal("Delete <strong>"+ids.length+"</strong> episodes from <strong>"+esc(S.season.name)+"</strong> watched by "+(S.assignedIds?"assigned users":"all users")+"?",async()=>{for(const id of ids){try{await api("/api/delete/"+id,{method:"DELETE"});}catch(e){}}await afterEpDelete();});}
function deleteSeason(seasonId,seriesId){const it=S.items[seasonId]||{name:"Season"};openDeleteModal("Delete <strong>"+esc(it.name)+"</strong> and all its episodes?",async()=>{await api("/api/delete/"+seasonId,{method:"DELETE"});route();});}
function deleteMovie(movieId){const it=S.items[movieId]||{name:"Movie"};openDeleteModal("Delete <strong>"+esc(it.name)+"</strong>?",async()=>{await api("/api/delete/"+movieId,{method:"DELETE"});nav("/lib/"+S.lib.id);});}

// Init
(async()=>{await checkAuth();try{const[u,l]=await Promise.all([api("/api/users"),api("/api/libraries")]);S.users=u;S.libraries=l;}catch(e){if(!e.message.includes("Unauthorized"))$el().innerHTML='<div class="empty-state">Failed to connect to Jellyfin</div>';return;}route();})();
