import {APPLY_STATUSES} from '../../constants/enums.js';
import {esc} from '../../utils/html.js';
export const applyBadge=(status)=>`<span class="badge ${esc(status)}">${esc(APPLY_STATUSES[status]||status||'未发起')}</span>`;
export function changeList(changes){
 if(!changes||!changes.length)return '<p class="readonly">节点相对冻结快照没有变化。</p>';
 return `<ul class="changes">${changes.map(c=>`<li><b>${esc(c.node)}</b> · ${esc(c.label||c.field)}：${esc(String(c.from))} → ${esc(String(c.to))}</li>`).join('')}</ul>`;
}
export function problemList(problems){
 if(!problems||!problems.length)return '';
 return `<ul class="changes blocked">${problems.map(p=>`<li>${esc(p)}</li>`).join('')}</ul>`;
}
export function nodeDiff(before,after){
 if(!after)return '<p class="readonly">尚未形成切换后的节点视图。</p>';
 const index=(list)=>{const m={};for(const n of list||[])m[n.id]=n;return m;};
 const b=index(before),a=index(after);
 const label={role:'角色',term:'任期',log_position:'日志位置',available:'可达',fenced:'写入隔离',vote_enabled:'投票权'};
 const fmt=v=>typeof v==='boolean'?(v?'是':'否'):esc(String(v??'—'));
 const rows=Object.keys(a).map(id=>{
  const before=b[id]||{},now=a[id];
  const cells=Object.keys(label).map(k=>before[k]!==now[k]?`<span class="delta">${label[k]} ${fmt(before[k])} → ${fmt(now[k])}</span>`:fmt(now[k]));
  return [esc(now.name),...cells];
 });
 return `<div class="table-wrap"><table><thead><tr>${['节点','角色','任期','日志位置','可达','写入隔离','投票权'].map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(v=>`<td>${v}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
