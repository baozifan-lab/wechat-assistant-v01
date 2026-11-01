export const $=id=>document.getElementById(id);
export const fmt=t=>new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(t));
export function toast(t){$('toast').textContent=t;$('toast').style.display='block';clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>$('toast').style.display='none',4500)}
export function node(tag,cls,text){const el=document.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=text;return el}
