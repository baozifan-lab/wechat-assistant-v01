let token = document.querySelector('meta[name="pilot-token"]').content;
let renewing = null;

async function needsCredential(response){
  if(![403,404].includes(response.status))return false;
  try{return (await response.clone().json()).error?.includes('缺少本机访问凭证')===true}catch{return false}
}

async function renewCredential(){
  if(renewing)return renewing;
  renewing=(async()=>{
    // Read only a fresh, same-origin local page. Parsing does not replace the
    // current form or execute scripts, so unsaved replies and keys stay intact.
    const response=await fetch('/',{cache:'no-store',credentials:'same-origin',redirect:'error'});
    if(!response.ok)throw Error('本机连接状态暂时无法更新，请稍后刷新页面。');
    const page=new DOMParser().parseFromString(await response.text(),'text/html');
    const fresh=page.querySelector('meta[name="pilot-token"]')?.content;
    if(!fresh)throw Error('页面连接状态无法更新，请刷新页面后重试。');
    token=fresh;
    document.querySelector('meta[name="pilot-token"]').content=fresh;
  })();
  try{await renewing}finally{renewing=null}
}

export async function apiRequest(path,options={}){
  const target=new URL(path,location.href);
  if(target.origin!==location.origin||!target.pathname.startsWith('/api/'))throw Error('只允许访问本机服务接口。');
  const send=async current=>{
    const headers=new Headers(options.headers||{});headers.set('X-Pilot-Token',current);
    return fetch(target.pathname+target.search,{...options,headers,cache:'no-store',credentials:'same-origin',redirect:'error'});
  };
  const sentToken=token;
  let response=await send(sentToken);
  if(await needsCredential(response)){
    if(sentToken===token)await renewCredential();
    if(sentToken===token)return response;
    // The authentication gate rejected the first request before business code
    // ran. Reuse the exact body/request ID and retry just once.
    response=await send(token);
    if(await needsCredential(response))throw Error('页面连接状态已失效，请刷新页面后重试。');
  }
  return response;
}

export async function api(path,data){
  const response=await apiRequest(path,{method:data?'POST':'GET',headers:{'Content-Type':'application/json'},...(data?{body:JSON.stringify(data)}:{})});
  let value;
  try{value=await response.json()}catch{throw Error('本机服务暂未返回可读结果，请稍后重试。')}
  if(!response.ok)throw Error(value.error||'操作暂未完成');
  return value;
}
