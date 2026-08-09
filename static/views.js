// ── Home page polling ─────────────────────────────────────────────────
let _homePolling=null, _lastWatchEventAt=null, _recentWatchSummary=null;
function startHomePolling(){
  stopHomePolling();
  _homePolling=setInterval(()=>{refreshActivity();checkWatchUpdates();},15000);
}
function stopHomePolling(){if(_homePolling){clearInterval(_homePolling);_homePolling=null;}}

function fmtMs(ms){if(!ms||ms<0)return'';const s=Math.floor(ms/1000),h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=s%60;return h?(h+':'+String(m).padStart(2,'0')+':'+String(ss).padStart(2,'0')):(m+':'+String(ss).padStart(2,'0'));}

function renderActivityHtml(streams){
  let h=`<div class="activity-section"><h3>Now Playing${streams.length?` <span style="font-size:0.8rem;color:var(--text-muted);font-weight:400">(${streams.length})</span>`:''}</h3>`;
  if(!streams.length){
    h+='<div style="color:var(--text-muted);font-size:0.82rem;padding:0.4rem 0">No active streams</div>';
  } else {
    h+='<div class="activity-cards-grid">';
    for(const s of streams){
      const remaining=s.duration&&s.viewOffset?fmtMs(s.duration-s.viewOffset):'';
      const streamBadge=s.streamType?`<span class="activity-stream-badge ${s.streamType==='Direct Play'?'direct':'transcode'}">${esc(s.streamType)}</span>`:'';
      const bwText=s.bandwidth!=null?`<span class="activity-sub">${s.bandwidth} Mbps</span>`:'';
      const isEp=s.type==='episode';
      const lib=s.librarySectionID||'';
      const gpk=s.grandparentRatingKey||'';
      const pk=s.parentRatingKey||'';
      const rk=s.ratingKey||'';
      const posterDest=isEp&&lib&&gpk?`nav('/lib/${lib}/s/${gpk}')`:lib&&rk?`nav('/lib/${lib}/m/${rk}')`:null;
      const epDest=isEp&&lib&&gpk&&pk&&rk?`/lib/${lib}/s/${gpk}/${pk}/e/${rk}`:null;
      const seaDest=isEp&&lib&&gpk&&pk?`/lib/${lib}/s/${gpk}/${pk}`:null;
      const serDest=isEp&&lib&&gpk?`/lib/${lib}/s/${gpk}`:null;
      let line1,line2='',line3='';
      if(isEp){
        // Line 1: TV show title → series page
        line1=s.seriesName?(serDest?`<div class="activity-title activity-link" onclick="nav('${serDest}')">${esc(s.seriesName)}</div>`:`<div class="activity-title">${esc(s.seriesName)}</div>`):'';
        // Line 2: Episode title → episode detail
        line2=`<div class="activity-ep-title${epDest?' activity-link':''}" ${epDest?`onclick="nav('${epDest}')"`:''}>${esc(s.title)}</div>`;
        // Line 3: Season · Episode number
        const sp=s.seasonName?(seaDest?`<span class="activity-link" onclick="nav('${seaDest}')">${esc(s.seasonName)}</span>`:esc(s.seasonName)):'';
        const ep=s.episodeNumber!=null?(epDest?`<span class="activity-link" onclick="nav('${epDest}')">E${s.episodeNumber}</span>`:`E${s.episodeNumber}`):'';
        const pts=[sp,ep].filter(Boolean);
        line3=pts.length?`<div class="activity-sub" style="margin-top:2px">${pts.join(' · ')}</div>`:'';
      }else{
        // Movie: title only → movie detail
        line1=posterDest?`<div class="activity-title activity-link" onclick="${posterDest}">${esc(s.title)}</div>`:`<div class="activity-title">${esc(s.title)}</div>`;
      }
      const posterHtml=s.imageUrl
        ?(posterDest?`<div class="activity-poster-link" onclick="${posterDest}"><img class="activity-poster" src="${s.imageUrl}" onerror="this.style.display='none'"></div>`:`<img class="activity-poster" src="${s.imageUrl}" onerror="this.style.display='none'">`)
        :'<div class="activity-poster"></div>';
      h+=`<div class="activity-card">
        ${posterHtml}
        <div class="activity-info">
          ${line1}${line2}${line3}
          <div class="activity-sub" style="margin-top:3px">${esc(s.player)}</div>
          <div class="activity-progress-bar"><div class="activity-progress-fill" style="width:${s.progress}%"></div></div>
          <div class="activity-sub" style="margin-top:3px">${s.progress}%${remaining?' · '+remaining+' left':''}</div>
          <div class="activity-user-row"><span class="activity-user-inline">${esc(s.user)}</span>${streamBadge}${bwText}</div>
        </div>
      </div>`;
    }
    h+='</div>';
  }
  h+='</div>';
  return h;
}

async function fetchRecentSummary(movies,episodes){
  const mIds=movies.map(m=>m.id).filter(Boolean).join(',');
  const eIds=episodes.map(e=>e.id).filter(Boolean).join(',');
  if(!mIds&&!eIds)return{};
  const params=[];
  if(mIds)params.push('movieIds='+mIds);
  if(eIds)params.push('episodeIds='+eIds);
  try{return await api('/api/watch-summary/items?'+params.join('&'));}catch(e){return{};}
}

async function refreshActivity(){
  const el=document.getElementById("activitySection");
  if(!el)return;
  try{const streams=await api("/api/activity");el.innerHTML=renderActivityHtml(streams);}catch(e){}
}

async function checkWatchUpdates(){
  if(location.hash&&location.hash!=='#/')return;
  try{
    const{last_at}=await api("/api/events/last-update");
    if(!last_at||last_at===_lastWatchEventAt)return;
    _lastWatchEventAt=last_at;
    const moviesLib=S.libraries.find(l=>l.type==='movies');
    const showsLib=S.libraries.find(l=>l.type==='tvshows');
    const[recentMovies,recentEpisodes]=await Promise.all([
      api("/api/recent/movies").catch(()=>[]),
      api("/api/recent/episodes").catch(()=>[])
    ]);
    _recentWatchSummary=await fetchRecentSummary(recentMovies,recentEpisodes);
    const mEl=document.getElementById("recentMoviesRow");
    const eEl=document.getElementById("recentEpisodesRow");
    if(mEl)mEl.innerHTML=renderRecentItems(recentMovies,'movie',moviesLib,_recentWatchSummary);
    if(eEl)eEl.innerHTML=renderRecentItems(recentEpisodes,'episode',showsLib,_recentWatchSummary);
  }catch(e){}
}

function renderRecentItems(items,type,lib,watchSummary){
  const typeLabel=type==='movie'?'MOVIE':'TV';
  const fallback=type==='movie'?'🎬':'📺';
  return items.map(item=>{
    const path=type==='movie'?`/lib/${lib?.id}/m/${item.id}`:`/lib/${lib?.id}/s/${item.seriesId}`;
    const title=type==='movie'?esc(item.name):esc(item.seriesName||item.name);
    const sub=type==='movie'?(item.year||''):`${esc(item.seasonName||'')} · E${item.episodeNumber}`;
    const ws=watchSummary&&watchSummary[item.id];
    const allWatched=ws&&ws.total>0&&ws.watched===ws.total;
    const anyWatched=ws&&ws.watched>0;
    let badgeHtml='';
    if(anyWatched){
      const watcherIds=(ws.watcher_ids||[]).map(String);
      const names=watcherIds.map(id=>S.users.find(u=>String(u.id)===id)?.name).filter(Boolean).join(', ');
      const cls=allWatched?'watch-count-badge complete':'watch-count-badge partial';
      const label=(allWatched?'✓ ':'')+ws.watched+'/'+ws.total;
      const titleTip=names||(watcherIds.length?watcherIds.length+' watcher'+(watcherIds.length!==1?'s':''):'');
      badgeHtml=`<div class="${cls}" data-watcher-ids="${escAttr(watcherIds.join(','))}" title="${escAttr(titleTip)}">${label}</div>`;
    }
    return `<div class="item-card" style="width:148px" onclick="nav('${path}')">
      ${badgeHtml}
      <div class="item-type-badge">${typeLabel}</div>
      <img class="item-poster" src="${item.imageUrl}" loading="lazy" onerror="this.style.display='none'">
      <div class="item-overlay"><div class="item-overlay-title">${title}</div><div class="item-overlay-year">${sub}</div></div>
    </div>`;
  }).join('');
}

// View Libraries (Home / Discover)
async function viewLibraries(){
  stopHomePolling();
  const el=$el();el.innerHTML='<div class="loading">Loading</div>';crumbs([]);
  try{
    const[users,libs,recentMovies,recentEpisodes,recentAdded,activity,lastUpdate]=await Promise.all([
      api("/api/users"),api("/api/libraries"),
      api("/api/recent/movies").catch(()=>[]),api("/api/recent/episodes").catch(()=>[]),
      api("/api/recent/added?limit=30").catch(()=>[]),
      api("/api/activity").catch(()=>[]),
      api("/api/events/last-update").catch(()=>({last_at:null}))
    ]);
    S.users=users;S.libraries=libs;S.lib=null;S.series=null;S.season=null;S.assignedIds=null;S.selectedItemIds.clear();_currentLibId=null;
    _lastWatchEventAt=lastUpdate.last_at;
    const vis=libs.filter(l=>l.monitored);
    const moviesLib=libs.find(l=>l.type==='movies');const showsLib=libs.find(l=>l.type==='tvshows');
    renderSidebar();

    _recentWatchSummary=await fetchRecentSummary(recentMovies,recentEpisodes);

    let html='';
    html+=`<div class="search-bar"><span class="icon">🔍</span><input type="text" id="globalSearch" placeholder="Search movies and TV shows..." oninput="onGlobalSearch(this.value)" autocomplete="off"></div>`;
    html+='<div id="globalSearchResults" style="display:none"></div>';
    html+='<div id="homeSections">';
    if(!vis.length){
      html+='<div class="empty-state">No monitored libraries. Click ⚙ Settings to configure.</div>';
    }
    html+=`<div id="activitySection">${renderActivityHtml(activity)}</div>`;
    if(recentAdded.length){
      html+=`<div style="margin-bottom:2rem"><div class="section-header"><h3>Recently Added</h3><a class="section-see-all" onclick="nav('/recently-added')">See All →</a></div><div id="recentlyAddedRow" class="recent-row">${renderRecentlyAddedItems(recentAdded)}</div></div>`;
    }
    if(recentMovies.length){
      html+=`<div style="margin-bottom:2rem"><div class="section-header"><h3>Recently Watched Movies</h3><a class="section-see-all" onclick="nav('/recent/movies')">See All →</a></div><div id="recentMoviesRow" class="recent-row">${renderRecentItems(recentMovies,'movie',moviesLib,_recentWatchSummary)}</div></div>`;
    }
    if(recentEpisodes.length){
      html+=`<div style="margin-bottom:2rem"><div class="section-header"><h3>Recently Watched Episodes</h3><a class="section-see-all" onclick="nav('/recent/episodes')">See All →</a></div><div id="recentEpisodesRow" class="recent-row">${renderRecentItems(recentEpisodes,'episode',showsLib,_recentWatchSummary)}</div></div>`;
    }
    html+='</div>';

    el.innerHTML=html;
    vis.forEach(l=>{S.items[l.id]={name:l.name,type:l.type};});
    startHomePolling();
  }catch(e){el.innerHTML='<div class="empty-state">Failed to connect.<br><small>'+esc(e.message)+'</small></div>';}
}

// Recently Watched All page
let _recentAllReqId=0;
async function viewRecentAll(type,page){
  stopHomePolling();
  page=page||1;
  if(type!=='movies'&&type!=='episodes'){viewLibraries();return;}
  const reqId=++_recentAllReqId;
  const isMovies=type==='movies';
  const title=isMovies?'Recently Watched Movies':'Recently Watched Episodes';
  const el=$el();el.innerHTML='<div class="loading">Loading</div>';
  crumbs([{label:'Home',hash:'/'},{label:title}]);
  try{
    const data=await api(`/api/recent/${type}?page=${page}`).catch(()=>({items:[],totalCount:0,page:1,pageSize:50,isSearch:false}));
    if(reqId!==_recentAllReqId)return;
    const items=data.items||[];
    if(!S.libraries.length)S.libraries=await api("/api/libraries").catch(()=>[]);
    if(reqId!==_recentAllReqId)return;
    const moviesLib=S.libraries.find(l=>l.type==='movies');
    const showsLib=S.libraries.find(l=>l.type==='tvshows');
    const lib=isMovies?moviesLib:showsLib;
    const itemType=isMovies?'movie':'episode';
    const ws=await fetchRecentSummary(isMovies?items:[],isMovies?[]:items);
    if(reqId!==_recentAllReqId)return;
    if(!items.length){
      el.innerHTML='<div class="empty-state">No recently watched items found.</div>';
      return;
    }
    const cardsHtml=renderRecentItems(items,itemType,lib,ws);
    el.innerHTML=`<h2 style="margin-bottom:1.25rem">${esc(title)}</h2><div class="recent-row" id="recentAllGrid" style="flex-wrap:wrap">${cardsHtml}</div>`;
    renderPagination(data,'/recent/'+type,page,'recentAllGrid');
  }catch(e){
    if(reqId!==_recentAllReqId)return;
    el.innerHTML='<div class="empty-state">Failed to load.<br><small>'+esc(e.message)+'</small></div>';
  }
}

// ── Recently Added (triage inbox for newly added movies & series) ──────
function renderRecentlyAddedItems(items){
  if(!items||!items.length)return '<div class="empty-state">Nothing new right now. Newly added movies and TV series will appear here.</div>';
  return items.map(item=>{
    const typeLabel=item.type==='movie'?'MOVIE':'TV';
    return `<div class="item-card" style="width:148px" onclick="openItemOverlay('${sanitizeIdForClient(item.libId)}','${item.type}','${sanitizeIdForClient(item.id)}')">
      <div class="item-type-badge">${typeLabel}</div>
      <img class="item-poster" src="/api/image/${item.id}?type=Primary&maxWidth=300" loading="lazy" onerror="this.style.display='none'">
      <div class="item-overlay"><div class="item-overlay-title">${esc(cleanName(item.name||''))}</div>${item.year?`<div class="item-overlay-year">${item.year}</div>`:''}</div>
    </div>`;
  }).join('');
}
// Re-fetch and re-render the list after an assignment is saved, so the title
// just triaged drops off without a full reload. No-op when neither view is open.
async function refreshRecentlyAddedRow(){
  const row=document.getElementById("recentlyAddedRow");
  const grid=document.getElementById("recentlyAddedGrid");
  if(!row&&!grid)return;
  try{
    const items=await api("/api/recent/added?limit="+(grid?200:30)).catch(()=>[]);
    if(row)row.innerHTML=renderRecentlyAddedItems(items);
    if(grid)grid.innerHTML=renderRecentlyAddedItems(items);
  }catch(e){}
}
// Recently Added "See All" page
async function viewRecentlyAdded(){
  stopHomePolling();
  const el=$el();el.innerHTML='<div class="loading">Loading</div>';
  crumbs([{label:'Home',hash:'/'},{label:'Recently Added'}]);
  try{
    if(!S.libraries.length)S.libraries=await api("/api/libraries").catch(()=>[]);
    if(!S.users.length)S.users=await api("/api/users").catch(()=>[]);
    const items=await api("/api/recent/added?limit=200").catch(()=>[]);
    el.innerHTML=`<h2 style="margin-bottom:0.4rem">Recently Added</h2>`
      +`<div class="assign-desc" style="margin-bottom:1.25rem">Newly added movies and TV series. Open a title to assign users — saving removes it from this list. Items also age off after the configured window.</div>`
      +`<div id="recentlyAddedGrid" class="recent-row" style="flex-wrap:wrap">${renderRecentlyAddedItems(items)}</div>`;
  }catch(e){el.innerHTML='<div class="empty-state">Failed to load.<br><small>'+esc(e.message)+'</small></div>';}
}

// Global search (home page)
const _doGlobalSearch=debounce(async(q)=>{
  const resEl=document.getElementById("globalSearchResults");
  const homeEl=document.getElementById("homeSections");
  if(!resEl||!homeEl)return;
  const query=q.trim();
  if(!query){
    resEl.style.display='none';resEl.innerHTML='';
    homeEl.style.display='';
    return;
  }
  homeEl.style.display='none';
  resEl.style.display='';
  resEl.innerHTML='<div class="loading">Searching</div>';
  try{
    const data=await api('/api/search?q='+encodeURIComponent(query));
    const input=document.getElementById("globalSearch");
    if(input&&input.value.trim()!==query)return;
    renderGlobalSearchResults(data.items||[],query);
  }catch(e){
    resEl.innerHTML='<div class="empty-state">Search failed: '+esc(e.message)+'</div>';
  }
},250);
function onGlobalSearch(q){_doGlobalSearch(q);}
function renderGlobalSearchResults(items,query){
  const resEl=document.getElementById("globalSearchResults");
  if(!resEl)return;
  if(!items.length){
    resEl.innerHTML=`<div class="empty-state">No results for "${esc(query)}".</div>`;
    return;
  }
  let h=`<div class="section-header"><h3>Search Results <span style="font-size:0.8rem;color:var(--text-muted);font-weight:400">(${items.length})</span></h3></div>`;
  h+='<div class="item-grid">';
  for(const it of items){
    const path=it.type==='movie'?`/lib/${it.libId}/m/${it.id}`:`/lib/${it.libId}/s/${it.id}`;
    const typeLabel=it.type==='movie'?'MOVIE':'TV';
    h+=`<div class="item-card" onclick="nav('${path}')">
      <div class="item-type-badge">${typeLabel}</div>
      <img class="item-poster" src="/api/image/${it.id}?type=Primary&maxWidth=300" loading="lazy" onerror="this.style.display='none'">
      <div class="item-overlay"><div class="item-overlay-title">${esc(cleanName(it.name))}</div>${it.year?`<div class="item-overlay-year">${it.year}</div>`:''}</div>
    </div>`;
  }
  h+='</div>';
  resEl.innerHTML=h;
}

// Grid with selection toolbar
let _currentGridItems=[], _currentLibId=null, _currentPage=1, _currentPaginationData=null;
let _activeGenre=null, _showOnlyWatched=false, _watchSummary=null, _watchSummaryLoading=false, _watchSummaryLibId=null;
async function viewGrid(libId,page){
  stopHomePolling();
  page=page||1;
  if(_currentLibId!==libId){_activeGenre=null;_showOnlyWatched=false;_watchSummary=null;_watchSummaryLibId=null;_currentPaginationData=null;}
  S.selectedItemIds.clear();
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
    renderPagination(data,'/lib/'+libId,page);
    if(_showOnlyWatched&&_watchSummary)applyWatchedFilter();
    attachSearchListener(libId);
    fetchGenres(libId,lib.type);
    fetchWatchSummary(libId,lib.type);
  }catch(e){el.innerHTML='<div class="empty-state">Error: '+esc(e.message)+'</div>';}
}
function renderGridItems(items,libId,lib){
  const typeLabel=lib.type==="movies"?"MOVIE":"TV";
  const pfx=lib.type==="movies"?"m":"s";
  const container=document.getElementById("gridItems");
  if(!container)return;
  const safeLibId=sanitizeIdForClient(libId);
  const displayItems=(_showOnlyWatched&&_watchSummary)?items.filter(s=>{const ws=_watchSummary[s.id];return ws&&ws.total>0&&ws.watched===ws.total;}):items;
  let h='';
  for(const s of displayItems){
    const isSelected=S.selectedItemIds.has(s.id);
    const ws=_watchSummary&&_watchSummary[s.id];
    const allWatched=ws&&ws.total>0&&ws.watched===ws.total;
    const anyWatched=ws&&ws.watched>0;
    let badgeHtml='';
    if(anyWatched){
      const watcherIds=(ws.watcher_ids||[]).map(String);
      const names=watcherIds.map(id=>S.users.find(u=>String(u.id)===id)?.name).filter(Boolean).join(', ');
      const cls=allWatched?'watch-count-badge complete':'watch-count-badge partial';
      const label=(allWatched?'✓ ':'')+ws.watched+'/'+ws.total;
      const titleTip=names||(watcherIds.length?watcherIds.length+' watcher'+(watcherIds.length!==1?'s':''):'');
      badgeHtml=`<div class="${cls}" data-watcher-ids="${escAttr(watcherIds.join(','))}" title="${escAttr(titleTip)}">${label}</div>`;
    }
    const overlayType=lib.type==="movies"?"movie":"series";
    const safeItemId=sanitizeIdForClient(s.id);
    h+=`<div class="item-card" data-id="${safeItemId}" onclick="openItemOverlay('${safeLibId}','${overlayType}','${safeItemId}')">
      <div class="checkbox-overlay"><input type="checkbox" class="grid-checkbox" data-id="${safeItemId}" ${isSelected?'checked':''} onclick="event.stopPropagation();toggleItemSelection('${safeItemId}',this.checked)"></div>
      ${badgeHtml}
      <div class="item-type-badge">${typeLabel}</div>
      <img class="item-poster" src="/api/image/${encodeURIComponent(safeItemId)}?type=Primary&maxWidth=300" loading="lazy" onerror="this.style.display='none'">
      <div class="item-overlay"><div class="item-overlay-title">${esc(cleanName(s.name))}</div>${s.year?`<div class="item-overlay-year">${s.year}</div>`:''}</div>
    </div>`;
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
function renderPagination(data,basePath,page,containerId){
  containerId=containerId||"gridItems";
  const container=document.getElementById(containerId)?.parentNode;
  if(!container)return;
  let pagHtml='';
  if(!data.isSearch){
    const tp=Math.ceil(data.totalCount/data.pageSize);
    if(tp>1){
      let nums='';
      for(const r of getPageRange(page,tp)){
        if(r==='...')nums+=`<span class="page-ellipsis">…</span>`;
        else nums+=`<button class="page-btn${r===page?' current':''}"${r===page?' disabled':''} onclick="nav('${basePath}/p/${r}')">${r}</button>`;
      }
      pagHtml=`<div class="pagination"><button ${page<=1?'disabled':''} onclick="nav('${basePath}/p/1')" title="First">«</button><button ${page<=1?'disabled':''} onclick="nav('${basePath}/p/${page-1}')" title="Previous">‹</button>${nums}<button ${page>=tp?'disabled':''} onclick="nav('${basePath}/p/${page+1}')" title="Next">›</button><button ${page>=tp?'disabled':''} onclick="nav('${basePath}/p/${tp}')" title="Last">»</button><span class="page-info">${data.totalCount} items · Page ${page} of ${tp}</span></div>`;
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
    renderPagination(data,'/lib/'+libId,1);
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
  const watchedIds=Object.entries(_watchSummary).filter(([,ws])=>ws&&ws.total>0&&ws.watched===ws.total).map(([k])=>k);
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
  const watchedCount=Object.values(_watchSummary).filter(ws=>ws&&ws.total>0&&ws.watched===ws.total).length;
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
    if(_currentPaginationData)renderPagination(_currentPaginationData,'/lib/'+_currentLibId,_currentPage);
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
  const safeLibId=escAttr(String(libId));
  let h=`<span class="genre-chip all-chip${!_activeGenre?' active':''}" data-genre="" data-libid="${safeLibId}">All</span>`;
  for(const g of genres){
    h+=`<span class="genre-chip${_activeGenre===g?' active':''}" data-genre="${escAttr(g)}" data-libid="${safeLibId}">${esc(g)}</span>`;
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
  h+='</div><div class="assign-actions"><button class="btn-save" onclick="saveAssign(\''+sanitizeIdForClient(itemId)+'\')">Save</button><button onclick="resetAssign(\''+sanitizeIdForClient(itemId)+'\')">Reset to All</button><span class="assign-status" id="assignStatus">Saved!</span></div></div>';
  return h;
}
function invalidateWatchSummaryCache(){_watchSummary=null;_watchSummaryLibId=null;_recentWatchSummary=null;}
async function saveAssign(itemId){
  const chips=document.querySelectorAll("#assignUsers .assign-chip");
  const on=[];chips.forEach(c=>{if(c.classList.contains("on"))on.push(c.dataset.uid);});
  if(on.length===S.users.length){await api("/api/assignments/"+itemId,{method:"DELETE"});S.assignedIds=null;}
  else{await api("/api/assignments/"+itemId,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({userIds:on})});S.assignedIds=on;}
  invalidateWatchSummaryCache();
  await refreshAfterAssign();
  showAssignSaved();
}
async function resetAssign(itemId){
  await api("/api/assignments/"+itemId,{method:"DELETE"});S.assignedIds=null;
  invalidateWatchSummaryCache();
  await refreshAfterAssign();
  showAssignSaved();
}
// refreshAfterAssign() re-renders the whole panel, which replaces the
// #assignStatus element — so the "Saved!" flash has to be applied to the
// freshly rendered element *after* the refresh, not the one about to be
// destroyed (otherwise the confirmation never appears).
function showAssignSaved(){
  const st=$("#assignStatus");
  if(st){st.classList.add("show");setTimeout(()=>st.classList.remove("show"),2000);}
}
async function refreshAfterAssign(){
  refreshRecentlyAddedRow();
  if(_itemOverlayState){
    await renderItemOverlay();
    if(_currentLibId&&S.lib)fetchWatchSummary(_currentLibId,S.lib.type);
  } else {
    route();
  }
}

// ── Item Detail Overlay (opens from library grid) ─────────────────────
let _itemOverlayState=null; // {libId, type:'movie'|'series', itemId}
async function openItemOverlay(libId,type,itemId){
  _itemOverlayState={libId,type,itemId};
  const ov=document.getElementById("itemDetailOverlay");
  if(!ov){nav('/lib/'+libId+'/'+(type==='movie'?'m':'s')+'/'+itemId);return;}
  document.getElementById("itemDetailContent").innerHTML='<div class="loading">Loading</div>';
  ov.classList.add("active");
  document.body.style.overflow='hidden';
  await renderItemOverlay();
}
function closeItemOverlay(){
  const ov=document.getElementById("itemDetailOverlay");
  if(ov)ov.classList.remove("active");
  document.body.style.overflow='';
  _itemOverlayState=null;
}
async function renderItemOverlay(){
  if(!_itemOverlayState)return;
  const ct=document.getElementById("itemDetailContent");
  if(!ct)return;
  const{libId,type,itemId}=_itemOverlayState;
  try{
    await ensureLib(libId);
    if(type==='movie'){await ensureItem(itemId);ct.innerHTML=await _buildMovieDetailHtml(itemId);}
    else{await ensureSeries(itemId);ct.innerHTML=await _buildSeriesDetailHtml(itemId);}
  }catch(e){ct.innerHTML='<div class="empty-state">Error: '+esc(e.message)+'</div>';}
}
async function _buildMovieDetailHtml(movieId){
  const it=S.items[movieId]||{name:"Movie"};
  const[ws,assign,adStatus]=await Promise.all([
    api("/api/watch-status/"+movieId),
    api("/api/assignments/"+movieId),
    api("/api/auto-delete/movie/"+movieId).catch(()=>null)
  ]);
  S.assignedIds=assign.mode==="custom"?assign.assigned:null;
  let displayUsers=ws;if(assign.mode==="custom"&&assign.assigned.length){const a=new Set(assign.assigned);displayUsers=ws.filter(u=>a.has(u.userId));}
  const allW=isWatchedByAssigned(ws,S.assignedIds);
  let h=renderAssignPanel(movieId,assign,false);
  h+='<div class="movie-detail"><div class="movie-detail-header"><div class="movie-detail-poster"><img src="/api/image/'+movieId+'?type=Primary&maxWidth=400" onerror="this.parentElement.innerHTML=\'🎬\'" alt=""></div><div class="movie-detail-info"><h2>'+esc(it.name)+(allW?'<span class="all-watched-tag">All Watched</span>':'')+'</h2>'+(it.year?'<div class="year">'+it.year+'</div>':'')+renderAutoDeleteBadge(adStatus,movieId,'movie')+'<div style="margin-top:1rem"><button class="btn-delete-bulk" onclick="deleteMovie(\''+sanitizeIdForClient(movieId)+'\')">Delete Movie</button></div></div></div><div class="movie-watch-list">';
  for(const u of displayUsers)h+='<div class="movie-watch-item">'+badge(u)+'</div>';
  h+='</div></div>';
  return h;
}
async function _buildSeriesDetailHtml(seriesId){
  const ser=S.series||{id:seriesId,name:"Series"};
  const libId=_itemOverlayState?_itemOverlayState.libId:(S.lib&&S.lib.id);
  const[seasons,assign,adStatus]=await Promise.all([
    api("/api/seasons/"+seriesId),
    api("/api/assignments/"+seriesId),
    api("/api/auto-delete/series/"+seriesId).catch(()=>null)
  ]);
  S.assignedIds=assign.mode==="custom"?assign.assigned:null;
  if(!seasons.length){return '<div class="empty-state">No seasons found.</div>';}
  seasons.forEach(s=>{S.items[s.id]={name:s.name,type:"season",seriesId};});
  const posterHtml=`<div style="display:flex; gap:1.5rem; margin-bottom:1.5rem; align-items:center;"><div style="width:120px; border-radius:8px; overflow:hidden; background:var(--bg-card); aspect-ratio:2/3; flex-shrink:0;"><img src="/api/image/${seriesId}?type=Primary&maxWidth=200" onerror="this.parentElement.innerHTML='📺'" style="width:100%; height:100%; object-fit:cover;"></div><div><h2 style="margin-bottom:0.25rem">${esc(cleanName(ser.name))}</h2><div class="lib-type">Series</div>${renderAutoDeleteBadge(adStatus,seriesId)}</div></div>`;
  let h=renderAssignPanel(seriesId,assign,true)+posterHtml+'<div class="season-list">';
  for(const s of seasons){
    const completedUsers=s.completedUsers||0,totalUsers=s.totalAssignedUsers||S.users.length,percent=totalUsers?(completedUsers/totalUsers)*100:0;
    let tooltipLines=[];if(s.userProgress&&s.userProgress.length){for(const up of s.userProgress){tooltipLines.push(`${up.userName}: ${up.playedCount}/${up.totalCount} ${up.completed?'✓':''}`);}}
    const tooltipText=tooltipLines.join('\n');
    h+=`<div class="season-item"><div style="display:flex;justify-content:space-between;align-items:center;"><span class="season-item-name" onclick="navFromOverlay('/lib/${sanitizeIdForClient(libId)}/s/${sanitizeIdForClient(seriesId)}/${sanitizeIdForClient(s.id)}')">${esc(s.name)}</span><button class="btn-delete" onclick="deleteSeason('${sanitizeIdForClient(s.id)}','${sanitizeIdForClient(seriesId)}')">Delete</button></div><div style="margin-top:8px;" title="${escAttr(tooltipText)}"><div style="display:flex;justify-content:space-between;font-size:0.75rem;color:var(--text-muted);margin-bottom:4px;"><span>${completedUsers}/${totalUsers} users completed</span><span>${Math.round(percent)}%</span></div><div style="background:var(--border);border-radius:4px;height:6px;overflow:hidden;"><div style="width:${percent}%;background:var(--green);height:100%;border-radius:4px;"></div></div></div></div>`;
  }
  h+='</div>';
  return h;
}
function navFromOverlay(hash){closeItemOverlay();nav(hash);}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&_itemOverlayState)closeItemOverlay();});
window.addEventListener('hashchange',()=>{if(_itemOverlayState)closeItemOverlay();});

// Movie detail
async function viewMovie(movieId){
  stopHomePolling();
  const el=$el();el.innerHTML='<div class="loading">Loading</div>';const lib=S.lib,it=S.items[movieId]||{name:"Movie"};
  crumbs([{label:"Libraries",hash:"/"},{label:lib.name,hash:"/lib/"+lib.id},{label:it.name}]);
  try{const[ws,assign,adStatus]=await Promise.all([api("/api/watch-status/"+movieId),api("/api/assignments/"+movieId),api("/api/auto-delete/movie/"+movieId).catch(()=>null)]);
    S.assignedIds=assign.mode==="custom"?assign.assigned:null;
    let displayUsers=ws;if(assign.mode==="custom"&&assign.assigned.length){const assignedSet=new Set(assign.assigned);displayUsers=ws.filter(u=>assignedSet.has(u.userId));}
    const allW=isWatchedByAssigned(ws,S.assignedIds);
    let h=renderAssignPanel(movieId,assign,false);
    h+='<div class="movie-detail"><div class="movie-detail-header"><div class="movie-detail-poster"><img src="/api/image/'+encodeURIComponent(movieId)+'?type=Primary&maxWidth=400" onerror="this.parentElement.innerHTML=\'🎬\'" alt=""></div><div class="movie-detail-info"><h2>'+esc(it.name)+(allW?'<span class="all-watched-tag">All Watched</span>':'')+'</h2>'+(it.year?'<div class="year">'+it.year+'</div>':'')+renderAutoDeleteBadge(adStatus,movieId,'movie')+'<div style="margin-top:1rem"><button class="btn-delete-bulk" onclick="deleteMovie(\''+sanitizeIdForClient(movieId)+'\')">Delete Movie</button></div></div></div><div class="movie-watch-list">';
    for(const u of displayUsers)h+='<div class="movie-watch-item">'+badge(u)+'</div>';
    h+='</div></div>';el.innerHTML=h;
  }catch(e){el.innerHTML='<div class="empty-state">Error: '+esc(e.message)+'</div>';}
}

// Seasons with poster & progress bars
async function viewSeasons(seriesId){
  stopHomePolling();
  const el=$el();el.innerHTML='<div class="loading">Loading</div>';const lib=S.lib,ser=S.series;S.season=null;
  crumbs([{label:"Libraries",hash:"/"},{label:lib.name,hash:"/lib/"+lib.id},{label:cleanName(ser.name)}]);
  try{const[seasons,assign,adStatus]=await Promise.all([api("/api/seasons/"+seriesId),api("/api/assignments/"+seriesId),api("/api/auto-delete/series/"+seriesId).catch(()=>null)]);
    S.assignedIds=assign.mode==="custom"?assign.assigned:null;
    if(!seasons.length){el.innerHTML='<div class="empty-state">No seasons found.</div>';return;}
    seasons.forEach(s=>{S.items[s.id]={name:s.name,type:"season",seriesId};});
    let posterHtml=`<div style="display:flex; gap:1.5rem; margin-bottom:1.5rem; align-items:center;"><div style="width:120px; border-radius:8px; overflow:hidden; background:var(--bg-card); aspect-ratio:2/3; flex-shrink:0;"><img src="/api/image/${seriesId}?type=Primary&maxWidth=200" onerror="this.parentElement.innerHTML='📺'" style="width:100%; height:100%; object-fit:cover;"></div><div><h2 style="margin-bottom:0.25rem">${esc(cleanName(ser.name))}</h2><div class="lib-type">Series</div>${renderAutoDeleteBadge(adStatus,seriesId)}</div></div>`;
    let h=renderAssignPanel(seriesId,assign,true)+posterHtml+'<div class="season-list">';
    for(const s of seasons){
      const totalEp=s.totalEpisodes||0,completedUsers=s.completedUsers||0,totalUsers=s.totalAssignedUsers||S.users.length,percent=totalUsers?(completedUsers/totalUsers)*100:0;
      let tooltipLines=[];if(s.userProgress&&s.userProgress.length){for(const up of s.userProgress){tooltipLines.push(`${up.userName}: ${up.playedCount}/${up.totalCount} ${up.completed?'✓':''}`);}}
      const tooltipText=tooltipLines.join('\n');
      h+=`<div class="season-item"><div style="display:flex;justify-content:space-between;align-items:center;"><span class="season-item-name" onclick="nav('/lib/${sanitizeIdForClient(lib.id)}/s/${sanitizeIdForClient(seriesId)}/${sanitizeIdForClient(s.id)}')">${esc(s.name)}</span><button class="btn-delete" onclick="deleteSeason('${sanitizeIdForClient(s.id)}','${sanitizeIdForClient(seriesId)}')">Delete</button></div><div style="margin-top:8px;" title="${escAttr(tooltipText)}"><div style="display:flex;justify-content:space-between;font-size:0.75rem;color:var(--text-muted);margin-bottom:4px;"><span>${completedUsers}/${totalUsers} users completed</span><span>${Math.round(percent)}%</span></div><div style="background:var(--border);border-radius:4px;height:6px;overflow:hidden;"><div style="width:${percent}%;background:var(--green);height:100%;border-radius:4px;"></div></div></div></div>`;
    }
    h+='</div>';el.innerHTML=h;
  }catch(e){el.innerHTML='<div class="empty-state">Error: '+esc(e.message)+'</div>';}
}

// Episodes
async function viewEpisodes(seriesId,seasonId){
  stopHomePolling();
  const el=$el();el.innerHTML='<div class="loading">Loading watch data</div>';const lib=S.lib,ser=S.series,sea=S.season;
  crumbs([{label:"Libraries",hash:"/"},{label:lib.name,hash:"/lib/"+lib.id},{label:cleanName(ser.name),hash:"/lib/"+lib.id+"/s/"+seriesId},{label:cleanName(sea.name)}]);
  try{const[episodes,assign]=await Promise.all([api("/api/season-watch-status/"+seriesId+"/"+seasonId),api("/api/assignments/"+seriesId)]);
    S.assignedIds=assign.mode==="custom"?assign.assigned:null;
    if(!episodes.length){
      // Do NOT auto-delete here. This is a read/navigation path, and a
      // transient Jellyfin hiccup can return an empty list for a season that
      // still has files on disk — deleting on that signal is irreversible.
      el.innerHTML='';
      const empty=document.createElement('div');empty.className='empty-state';
      empty.appendChild(document.createTextNode('No episodes found in this season.'));
      empty.appendChild(document.createElement('br'));
      const delBtn=document.createElement('button');delBtn.className='btn-delete';delBtn.style.marginTop='1rem';delBtn.textContent='Delete empty season';
      delBtn.addEventListener('click',()=>deleteSeason(seasonId,seriesId));
      const backBtn=document.createElement('button');backBtn.className='btn-cancel';backBtn.style.marginTop='1rem';backBtn.style.marginLeft='.5rem';backBtn.textContent='Back to series';
      backBtn.addEventListener('click',()=>nav('/lib/'+lib.id+'/s/'+encodeURIComponent(seriesId)));
      empty.appendChild(delBtn);empty.appendChild(backBtn);
      el.appendChild(empty);
      return;
    }
    S.episodes={};episodes.forEach(ep=>{S.episodes[ep.id]={name:ep.name,index:ep.indexNumber,allWatched:isWatchedByAssigned(ep.users,S.assignedIds)};});
    let uNames=[];if(assign.mode==="custom"&&assign.assigned.length){const assignedSet=new Set(assign.assigned);if(episodes[0]?.users){uNames=episodes[0].users.filter(u=>assignedSet.has(u.userId)).map(u=>u.userName);}}else{if(episodes[0]?.users)uNames=episodes[0].users.map(u=>u.userName);}
    const total=episodes.length,watchedAll=Object.values(S.episodes).filter(e=>e.allWatched).length;
    let posterHtml=`<div style="display:flex; gap:1.5rem; margin-bottom:1.5rem; align-items:center;"><div style="width:120px; border-radius:8px; overflow:hidden; background:var(--bg-card); aspect-ratio:2/3; flex-shrink:0;"><img src="/api/image/${encodeURIComponent(seasonId)}?type=Primary&maxWidth=200" onerror="this.parentElement.innerHTML='📺'" style="width:100%; height:100%; object-fit:cover;"></div><div><h2 style="margin-bottom:0.25rem">${esc(cleanName(sea.name))}</h2><div class="lib-type">${esc(cleanName(ser.name))}</div></div></div>`;
    const epWord=total===1?'episode':'episodes';
    let h=posterHtml+'<div class="watch-summary"><div><strong>'+watchedAll+'</strong> of <strong>'+total+'</strong> '+epWord+' watched by '+(S.assignedIds?'assigned users':'all users')+(watchedAll===total?' — <span style="color:var(--green)">safe to delete</span>':'')+'</div>'+(watchedAll>0?'<button class="btn-delete-bulk" onclick="deleteAllWatched()">Delete '+watchedAll+' watched</button>':'')+'</div>';
    h+='<div class="selection-toolbar"><button onclick="selectAll()">Select All</button><button onclick="selectWatched()">Select Watched</button><button onclick="selectNone()">Deselect All</button><span id="selCount" style="color:var(--text-muted)">0 selected</span><button class="delete-selected" id="btnDelSel" disabled onclick="deleteSelected()">Delete Selected</button></div>';
    h+='<div class="table-wrap"><table class="episode-table"><thead><tr><th style="width:30px"><input type="checkbox" class="ep-checkbox" onchange="toggleAll(this.checked)"></th><th>Episode</th>'+uNames.map(n=>'<th class="watch-cell">'+esc(n)+'</th>').join('')+'<th>Actions</th></tr></thead><tbody>';
    for(const ep of episodes){const allD=S.episodes[ep.id].allWatched,umap={};ep.users.forEach(u=>{umap[u.userName]=u;});h+='<tr id="ep-'+ep.id+'"><td><input type="checkbox" class="ep-checkbox" data-ep-id="'+ep.id+'" onchange="updateSelCount()"></td><td><span class="ep-num">E'+String(ep.indexNumber).padStart(2,"0")+'</span><span class="ep-name">'+esc(ep.name)+'</span>'+(ep.runTimeTicks?'<span class="ep-runtime">('+fmtT(ep.runTimeTicks)+')</span>':'')+(allD?'<span class="all-watched-tag">All</span>':'')+'</td>';for(const n of uNames){const u=umap[n];h+='<td class="watch-cell">'+(u?badge(u):'<span class="watch-badge unwatched">—</span>')+'</td>';}h+='<td><button class="btn-delete" onclick="deleteEpisode(\''+sanitizeIdForClient(ep.id)+'\')">Delete</button></td>';}
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
function deleteEpisode(id){const ep=S.episodes[id]||{name:id,index:0};openDeleteModal("Delete E"+String(ep.index).padStart(2,"0")+" - "+ep.name+"?",async()=>{await api("/api/delete/"+id,{method:"DELETE"});await afterEpDelete();});}
function deleteSelected(){const ids=getChecked();if(!ids.length)return;const names=ids.map(id=>{const ep=S.episodes[id];return ep?"E"+String(ep.index).padStart(2,"0")+" - "+ep.name:id;});const preview=names.length<=5?names.map(n=>"• "+n).join("\n"):names.slice(0,5).map(n=>"• "+n).join("\n")+"\n...and "+(names.length-5)+" more";openDeleteModal("Delete "+ids.length+" episodes?\n\n"+preview,async()=>{await api("/api/delete-batch",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({itemIds:ids})});await afterEpDelete();});}
function deleteAllWatched(){if(!S.season)return;const ids=Object.entries(S.episodes).filter(([,e])=>e.allWatched).map(([id])=>id);if(!ids.length)return;openDeleteModal("Delete "+ids.length+" episodes from "+S.season.name+" watched by "+(S.assignedIds?"assigned users":"all users")+"?",async()=>{for(const id of ids){try{await api("/api/delete/"+id,{method:"DELETE"});}catch(e){}}await afterEpDelete();});}
function deleteSeason(seasonId,seriesId){const it=S.items[seasonId]||{name:"Season"};openDeleteModal("Delete "+it.name+" and all its episodes?",async()=>{await api("/api/delete/"+seasonId,{method:"DELETE"});invalidateWatchSummaryCache();if(_itemOverlayState){await renderItemOverlay();if(_currentLibId&&S.lib)fetchWatchSummary(_currentLibId,S.lib.type);}else{route();}});}
function deleteMovie(movieId){const it=S.items[movieId]||{name:"Movie"};openDeleteModal("Delete "+it.name+"?",async()=>{await api("/api/delete/"+movieId,{method:"DELETE"});invalidateWatchSummaryCache();if(_itemOverlayState){closeItemOverlay();route();}else{nav("/lib/"+S.lib.id);}});}

// Episode detail
async function viewEpisodeDetail(libId,seriesId,seasonId,episodeId){
  stopHomePolling();
  const el=$el();el.innerHTML='<div class="loading">Loading</div>';
  const lib=S.lib||{id:libId,name:"Library"};
  const ser=S.series||{id:seriesId,name:"Series"};
  const sea=S.season||{id:seasonId,name:"Season"};
  const safeEpisodeId=encodeURIComponent(String(episodeId||''));
  try{
    const[epItem,ws,assign]=await Promise.all([
      api("/api/item/"+safeEpisodeId),
      api("/api/watch-status/"+safeEpisodeId),
      api("/api/assignments/"+seriesId)
    ]);
    S.assignedIds=assign.mode==="custom"?assign.assigned:null;
    const epName=epItem.name||"Episode";
    crumbs([
      {label:"Libraries",hash:"/"},
      {label:lib.name,hash:"/lib/"+libId},
      {label:cleanName(ser.name),hash:"/lib/"+libId+"/s/"+seriesId},
      {label:cleanName(sea.name),hash:"/lib/"+libId+"/s/"+seriesId+"/"+seasonId},
      {label:epName}
    ]);
    let displayUsers=ws;
    if(assign.mode==="custom"&&assign.assigned.length){const assignedSet=new Set(assign.assigned);displayUsers=ws.filter(u=>assignedSet.has(u.userId));}
    const allW=isWatchedByAssigned(ws,S.assignedIds);
    let h=`<div class="movie-detail"><div class="movie-detail-header"><div class="movie-detail-poster"><img src="/api/image/${safeEpisodeId}?type=Primary&maxWidth=400" onerror="this.parentElement.innerHTML='📺'" alt=""></div><div class="movie-detail-info"><h2>${esc(epName)}${allW?'<span class="all-watched-tag">All Watched</span>':''}</h2><div class="year">${esc(cleanName(ser.name))} · ${esc(cleanName(sea.name))}</div></div></div><div class="movie-watch-list">`;
    for(const u of displayUsers)h+='<div class="movie-watch-item">'+badge(u)+'</div>';
    h+='</div></div>';
    el.innerHTML=h;
  }catch(e){el.innerHTML='<div class="empty-state">Error: '+esc(e.message)+'</div>';}
}

// ── Auto-delete per-show controls ────────────────────────────────────────
function renderAutoDeleteBadge(adStatus, id, type='series'){
  if(!adStatus) return '';
  const ov=adStatus.override; // null | true | false
  const eff=adStatus.effective;
  const globalOn=adStatus.global_enabled;
  const graceDays=adStatus.grace_days!=null?adStatus.grace_days:1;
  const graceNote=graceDays===0?' — deletes immediately':(graceDays===1?' — 1 day grace period':` — ${graceDays} day grace period`);
  const itemWord=type==='movie'?'movie':'show';
  const setterFn=type==='movie'?'setAutoDeleteMovie':'setAutoDeleteSeries';
  let label,style,nextEnabled,nextLabel,clearBtn='';
  if(ov===true){
    if(globalOn){
      label=`Auto-delete: ON (override)${graceNote}`;style='color:var(--green)';
    } else {
      label='Auto-delete: Paused (global off) — override saved';style='color:var(--text-muted)';
    }
    nextEnabled=false;nextLabel=`Disable for this ${itemWord}`;
    clearBtn=`<button class="btn-cancel" style="margin-left:0.4rem;padding:0.15rem 0.5rem;font-size:0.72rem" onclick="${setterFn}('${sanitizeIdForClient(id)}',null)">Clear override</button>`;
  } else if(ov===false){
    label='Auto-delete: OFF (override)';style='color:var(--red)';
    nextEnabled=true;nextLabel=`Enable for this ${itemWord}`;
    clearBtn=`<button class="btn-settings" style="margin-left:0.4rem;padding:0.15rem 0.5rem;font-size:0.72rem" onclick="${setterFn}('${sanitizeIdForClient(id)}',null)">Clear override</button>`;
  } else if(eff){
    if(globalOn){
      label=`Auto-delete: ON (library)${graceNote}`;style='color:var(--green)';
    } else {
      label='Auto-delete: Paused (global off) — library default';style='color:var(--text-muted)';
    }
    nextEnabled=false;nextLabel=`Disable for this ${itemWord}`;
  } else {
    label=globalOn?'Auto-delete: OFF (library default)':'Auto-delete: OFF';
    style='color:var(--text-muted)';
    nextEnabled=true;nextLabel=`Enable for this ${itemWord}`;
  }
  const sweepBtn=globalOn&&(ov===true||eff)?`<button class="btn-settings" style="margin-left:0.4rem;padding:0.15rem 0.5rem;font-size:0.72rem" onclick="runAutoDeleteSweep()">Run sweep now</button>`:'';
  return `<div style="margin-top:0.5rem;font-size:0.78rem;${style}">${label}
    <button class="btn-settings" style="margin-left:0.4rem;padding:0.15rem 0.5rem;font-size:0.72rem" onclick="${setterFn}('${sanitizeIdForClient(id)}',${nextEnabled})">${nextLabel}</button>${clearBtn}${sweepBtn}</div>`;
}
async function setAutoDeleteSeries(seriesId,enabled){
  const resp=await api('/api/auto-delete/series/'+seriesId,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});
  if(resp&&resp.global_enabled_was_off){
    alert('Auto-delete enabled for this show.\nGlobal auto-delete has also been turned on automatically.');
  }
  if(_itemOverlayState)await renderItemOverlay();else route();
}
async function setAutoDeleteMovie(movieId,enabled){
  const resp=await api('/api/auto-delete/movie/'+movieId,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});
  if(resp&&resp.global_enabled_was_off){
    alert('Auto-delete enabled for this movie.\nGlobal auto-delete has also been turned on automatically.');
  }
  if(_itemOverlayState)await renderItemOverlay();else route();
}
async function runAutoDeleteSweep(){
  try{
    const resp=await api('/api/auto-delete/sweep',{method:'POST'});
    alert(resp&&resp.success?'Sweep complete. Anything eligible has been deleted — refresh to see changes.':'Sweep failed: '+(resp&&resp.error||'unknown error'));
    route();
  }catch(e){alert('Sweep failed: '+e.message);}
}

// Badge hover tooltip — resolves watcher names at hover time
(function(){
  let tip=null,tipBadge=null;
  function removeTip(){if(tip){tip.remove();tip=null;tipBadge=null;}}
  document.addEventListener('mouseover',function(e){
    const b=e.target.closest('.watch-count-badge');
    if(!b){removeTip();return;}
    if(b===tipBadge)return;
    removeTip();
    const ids=(b.getAttribute('data-watcher-ids')||'').split(',').filter(Boolean);
    if(!ids.length)return;
    const names=ids.map(id=>(S.users||[]).find(u=>String(u.id)===String(id))?.name).filter(Boolean);
    const txt=names.length?names.join(', '):ids.length+' watcher'+(ids.length!==1?'s':'');
    tip=document.createElement('div');
    tip.className='badge-tooltip';
    tip.textContent=txt;
    document.body.appendChild(tip);
    tipBadge=b;
    const r=b.getBoundingClientRect();
    const tw=tip.offsetWidth,th=tip.offsetHeight;
    tip.style.left=Math.min(Math.max(r.left+r.width/2-tw/2,8),window.innerWidth-tw-8)+'px';
    tip.style.top=(r.top+window.scrollY-th-6)+'px';
  });
  document.addEventListener('mouseout',function(e){
    if(e.target.closest('.watch-count-badge')&&!e.relatedTarget?.closest('.watch-count-badge'))removeTip();
  });
})();

// Init
(async()=>{if(!await checkAuth())return;try{const[u,l]=await Promise.all([api("/api/users"),api("/api/libraries")]);S.users=u;S.libraries=l;}catch(e){if(!e.message.includes("Unauthorized"))$el().innerHTML='<div class="empty-state">Failed to connect to Jellyfin</div>';return;}route();})();
