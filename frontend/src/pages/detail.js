import {store} from '../stores/entities.js';
import {table} from '../components/common/table.js';
import {badge} from '../components/common/status.js';
import {applyBadge} from '../components/common/apply.js';
import {esc} from '../utils/html.js';
import {download} from '../api/client.js';
import {message} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {appliesAPI} from '../api/entities.js';

export function detailPage(job){
 if(!job)return '<section class="empty"><h2>还没有演练记录</h2><p>先在演练队列提交一条任务，随后可查看每项判定证据。</p><a href="#drills">前往演练队列 →</a></section>';
 const r=job.result;
 const apply=store.applies.find(a=>a.drill_id===job.id);
 const canApply=session.role==='admin'||session.role==='operator';
 const applyLine=apply?`<p class="group-hint">落实状态：${applyBadge(apply.status)} ${apply.status==='applied'?'原主已隔离，新主已切换。':'<a href="#applies">查看落实凭据与前后节点 →</a>'}</p>`:'';
 const applyButton=(r&&r.decision==='approved'&&canApply&&(!apply||apply.status==='rejected'))?`<button id="apply">${apply&&apply.status==='rejected'?'重新复核并落实':'落实这次选主结论'}</button>`:'';
 return `<div class="section-title"><h2>${esc(job.label)} <small>#${job.id}</small></h2>${badge(job.status)}</div>${r?`<section class="verdict"><div><p>演练决策</p><h2>${r.decision==='approved'?'允许切主':r.decision==='blocked'?'安全阻断':'计算失败'}</h2></div><p>${r.winner?'候选节点 '+esc(r.winner):esc(r.error||'检查下方规则，调整输入后重新提交')}<br>新任期 ${r.new_term??'—'} · 多数派 ${r.reachable_votes??'—'}/${r.quorum??'—'}</p></section>${applyLine}${table(['规则','判定','输入证据'],(r.checks||[]).map(c=>[esc(c.rule),c.pass?'通过':'阻断',esc(c.evidence)]))}<h2>候选节点复核</h2>${table(['节点','日志位置','落后条数','满足候选条件'],(r.candidates||[]).map(n=>[esc(n.name),n.log_position,n.lag,n.eligible?'是':'否']))}<p class="checksum">输入摘要 ${esc(r.input_sha256||'无')}</p>`:'<p class="notice">后台正在计算，点击刷新获取状态。</p>'}<div class="toolbar"><button id="export">导出冻结输入与结果</button><button class="secondary" id="refresh">刷新状态</button>${applyButton}</div><details><summary>查看冻结输入</summary><pre>${esc(JSON.stringify(job.snapshot,null,2))}</pre></details>`;
}
export function bindDetail(job,render){
 document.querySelector('#refresh')?.addEventListener('click',render);
 document.querySelector('#export')?.addEventListener('click',()=>download(job.id).catch(e=>message(e.message,true)));
 document.querySelector('#apply')?.addEventListener('click',async e=>{
  const btn=e.currentTarget;btn.disabled=true;
  try{
   const {status,data}=await appliesAPI.apply(job.id);
   if(data.status==='applied'){location.hash='#applies';return;}
   await render();
   if(status===202)message('已有落实正在处理，请稍后刷新查看。');
   else message((data.status==='conflict'?'落实暂时冲突，可重试：':'落实被拒绝：')+(data.problems||[data.detail||'原因未知']).join('；'),true);  }catch(err){message(err.message,true);btn.disabled=false;}
 });
}
