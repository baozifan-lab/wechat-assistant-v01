const token = document.querySelector('meta[name="pilot-token"]').content;
export async function api(path,data){let r=await fetch(path,{method:data?'POST':'GET',headers:{'Content-Type':'application/json','X-Pilot-Token':token},...(data?{body:JSON.stringify(data)}:{})});let value=await r.json();if(!r.ok)throw Error(value.error||'操作暂未完成');return value}
