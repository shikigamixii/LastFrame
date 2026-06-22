const S={users:[],libraries:[],config:{},lib:null,series:null,season:null,items:{},episodes:{},assignedIds:null,selectedItemIds:new Set(),bulkAssignItems:[]};
const $=s=>document.querySelector(s),$el=()=>$("#content"),$bc=()=>$("#breadcrumbs");

function csrfToken(){const m=document.querySelector('meta[name="csrf-token"]');return m?m.content:'';}
async function api(p,o){
  o=o||{};
  const m=(o.method||'GET').toUpperCase();
  if(m!=='GET'&&m!=='HEAD'){
    o.headers=Object.assign({'X-CSRFToken':csrfToken()},o.headers||{});
  }
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
function fmtTs(iso){if(!iso)return"";try{const tz=S.config&&S.config.timezone;return new Date(iso).toLocaleString("en-US",{timeZone:tz||undefined,month:"short",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit"});}catch(e){return iso;}}
function esc(s){const d=document.createElement("div");d.textContent=s;return d.innerHTML;}
// esc() is safe for element *content* but does NOT escape quotes, so it can't
// be used inside HTML attribute values (a " in a media title/username would
// break out of the attribute). Use escAttr() for anything interpolated into
// an attribute: title="...", value="...", data-*="...".
function escAttr(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
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

// Sidebar
function renderSidebar(){
  const nav=document.getElementById("sidebarNav");
  if(!nav)return;
  const hash=location.hash||'#/';
  let h=`<div class="sidebar-nav-item${hash==='#/'||!hash?' active':''}" onclick="nav('/')"><span class="nav-icon">⌂</span><span class="nav-label">Home</span></div>`;
  const vis=(S.libraries||[]).filter(l=>l.monitored);
  for(const lib of vis){
    const libHash='#/lib/'+lib.id;
    const active=hash.startsWith(libHash)?' active':'';
    const icon=lib.type==='tvshows'?'📺':'🎬';
    h+=`<div class="sidebar-nav-item${active}" onclick="nav('/lib/${lib.id}')"><span class="nav-icon">${icon}</span><span class="nav-label">${esc(lib.name)}</span></div>`;
  }
  nav.innerHTML=h;
}

// Mobile sidebar drawer
function toggleSidebar(){const sb=document.getElementById("sidebar"),bd=document.getElementById("sidebarBackdrop");if(!sb)return;const open=sb.classList.toggle("open");if(bd)bd.classList.toggle("open",open);}
function closeSidebar(){const sb=document.getElementById("sidebar"),bd=document.getElementById("sidebarBackdrop");if(sb)sb.classList.remove("open");if(bd)bd.classList.remove("open");}

// Routing
function nav(hash){closeSidebar();history.pushState(null,'','#'+hash);renderSidebar();route();}
async function route(){
  const h=(location.hash||'#/').slice(1),p=h.split('/').filter(Boolean);
  if(!p.length)return viewLibraries();
  if(p[0]==='lib'&&p[1]){
    await ensureLib(p[1]);
    if(p.length===2)return viewGrid(p[1],1);
    if(p[2]==='p')return viewGrid(p[1],parseInt(p[3])||1);
    if(p[2]==='s'&&p[3]){await ensureSeries(p[3]);if(!p[4])return viewSeasons(p[3]);await ensureSeason(p[4],p[3]);if(p[5]==='e'&&p[6])return viewEpisodeDetail(p[1],p[3],p[4],p[6]);return viewEpisodes(p[3],p[4]);}
    if(p[2]==='m'&&p[3]){await ensureItem(p[3]);return viewMovie(p[3]);}
  }
  viewLibraries();
}
window.addEventListener('hashchange',()=>{renderSidebar();route();});
window.addEventListener('popstate',()=>{renderSidebar();route();});
async function ensureLib(id){if(S.lib?.id===id)return;if(!S.libraries.length)S.libraries=await api("/api/libraries");const l=S.libraries.find(x=>x.id===id);if(l){S.lib={id:l.id,name:l.name,type:l.type};return;}const i=await api("/api/item/"+id);S.lib={id,name:i.name,type:i.collectionType==="tvshows"?"tvshows":"movies"};}
async function ensureSeries(id){if(S.series?.id===id)return;const i=await api("/api/item/"+id);S.series={id,name:i.name};S.items[id]={name:i.name,type:"series"};}
async function ensureSeason(id,sid){if(S.season?.id===id)return;const i=await api("/api/item/"+id);S.season={id,name:i.name,seriesId:sid};S.items[id]={name:i.name,type:"season",seriesId:sid};}
async function ensureItem(id){if(S.items[id])return;const i=await api("/api/item/"+id);S.items[id]={name:i.name,type:i.type==="Movie"?"movie":"unknown"};}

// Debounce
function debounce(fn,ms){let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms);};}
