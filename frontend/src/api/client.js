import {session} from '../stores/session.js';
const headers=()=>({'Content-Type':'application/json','Authorization':'Bearer '+session.token});
function fail(status,data,path){if(status===401 && path!=='/auth/login'){session.clear();location.hash='#login';}throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||'请求失败'));}
export async function api(path, options={}){
 const response=await fetch('/api'+path,{...options,headers:{...headers(),...options.headers}});
 const data=await response.json().catch(()=>({}));
 if(!response.ok)fail(response.status,data,path);
 return data;
}
/** 返回 HTTP 状态和已解析内容；用于“已落实 / 待处理 / 拒绝”这类非错误语义的 202/409。 */
export async function apiResult(path, options={}){
 const response=await fetch('/api'+path,{...options,headers:{...headers(),...options.headers}});
 const data=await response.json().catch(()=>({}));
 if(response.status===401) fail(401,data,path);
 if(!response.ok && response.status!==409 && response.status!==202) fail(response.status,data,path);
 return {status:response.status,data};
}
export async function download(id){const r=await fetch('/api/drills/'+id+'/export',{headers:headers()});if(!r.ok)throw new Error('导出失败');const u=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=u;a.download='drill-'+id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);}
