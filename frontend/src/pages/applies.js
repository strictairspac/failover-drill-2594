import {store} from '../stores/entities.js';
import {session} from '../stores/session.js';
import {appliesAPI} from '../api/entities.js';
import {table} from '../components/common/table.js';
import {applyBadge,changeList,problemList,nodeDiff} from '../components/common/apply.js';
import {esc} from '../utils/html.js';
import {message} from '../hooks/action.js';

const canApply=()=>session.role==='admin'||session.role==='operator';
const time=v=>v?String(v).replace('T',' ').slice(0,19):'—';

export function appliesPage(){
 const byDrill={};for(const a of store.applies)byDrill[a.drill_id]=a;
 const candidates=store.drills.filter(d=>d.status==='completed'&&d.result?.decision==='approved');
 const pending=store.applies.filter(a=>a.status==='pending'||a.status==='applying');
 const done=store.applies.filter(a=>a.status==='applied');
 const rejected=store.applies.filter(a=>a.status==='rejected');
 const group=(title,hint,rows,emptyText)=>`<section class="apply-group"><div class="section-title"><h2>${title}</h2><span>${rows.length} 条</span></div><p class="group-hint">${hint}</p>${rows.length?`<ul class="apply-list">${rows.map(applyCard).join('')}</ul>`:`<p class="readonly">${emptyText}</p>`}</section>`;
 return `<section class="split compact"><div><h2>落实选主结论</h2><p>演练给出“允许切主”后，把结论落实到沙盘节点：原主降级并隔离，新主取得角色与任期，凭据随同一事务提交。落实前会按当前节点重新复核原证据。</p><p class="big-note">原证据失效，就拒绝并说明变化。</p></div>${canApply()?'<p class="readonly">选择下方仍可落实的演练，点击“落实结论”。同一次演练重试不会重复提升任期；证据失效时保留原结论并记录拒绝原因。</p>':'<p class="readonly">观察员可查看待处理、已落实与已拒绝记录。发起落实需要操作员或管理员。</p>'}</section><div class="section-title"><h2>可落实演练</h2><span>${candidates.length} 条已给出允许切主</span></div>${candidates.length?table(['演练','候选','目标任期','当前落实状态','操作'],candidates.map(d=>{const a=byDrill[d.id];return [esc(d.label)+` <small>#${d.id}</small>`,esc(d.result?.winner||'—'),d.result?.new_term??'—',applyBadge(a?.status),canApply()&&(!a||a.status==='rejected')?`<button class="secondary apply-btn" data-drill="${d.id}">${a&&a.status==='rejected'?'重新复核并落实':'落实结论'}</button>`:'—'];} )):'<p class="readonly">还没有“允许切主”的演练结果。先在演练队列提交并等待后台计算。</p>'}${group('待处理','已发起但尚未形成结论的落实记录；中断后由后台按原操作恢复。',pending,'没有待处理的落实记录。')}${group('已落实','原主已隔离，新主角色与任期已切换；下面展示落实前后的节点。',done,'还没有已落实的演练。')}${group('已拒绝','原证据不再安全时拒绝落实；原演练结论保持不变。',rejected,'没有被拒绝的落实记录。')}`;
}

function applyCard(a){
 const after=a.after_nodes;
 return `<li class="apply-card" data-apply="${a.id}"><div class="apply-head"><div><b>演练 #${a.drill_id}</b> ${applyBadge(a.status)}</div><span class="muted-text">${esc(a.actor||'—')} · 尝试 ${a.attempts||0} 次 · ${time(a.applied_at||a.updated_at)}</span></div>${a.status==='rejected'?`<p class="reject-note">拒绝原因（原演练结论保留）：</p>${problemList(a.problems)}<p class="reject-note">冻结快照 vs 拒绝当时的节点（高亮为差异）：</p>${nodeDiff(a.before_nodes,a.observed_nodes)}<p class="reject-note">相对冻结快照的变化：</p>${changeList(a.changes)}`:`<p class="group-hint">凭据 <code>${esc(a.credential||'—')}</code></p>`}${a.status==='applied'?`<p class="group-hint">新任期 ${esc(String(after?.find(n=>n.role==='leader')?.term??'—'))}</p><details open><summary>落实前后节点</summary>${nodeDiff(a.before_nodes,after)}</details>`:''}</li>`;
}

export function bindApplies(render){
 document.querySelectorAll('.apply-btn').forEach(btn=>btn.addEventListener('click',async()=>{
  const id=Number(btn.dataset.drill);btn.disabled=true;
  try{
   const {status,data}=await appliesAPI.apply(id);
   await render();
   if(data.status==='applied')message('已落实：原主已隔离，新主角色与任期已切换。');
   else if(status===202)message('已有落实正在处理，请稍后刷新查看。');
   else message((data.status==='conflict'?'落实暂时冲突，可重试：':'落实被拒绝：')+(data.problems||[data.detail||'原因未知']).join('；'),true);
  }catch(err){message(err.message,true);btn.disabled=false;}
 }));
}
