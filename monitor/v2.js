'use strict';
let lastV2=null;
const v2Names={breakout:'趋势突破',pullback:'趋势回调',reversion:'均值回归',trend_mix:'突破＋回调',all_mix:'三机制组合',conservative:'20% / 风险0.25%',moderate:'30% / 风险0.375%',expanded:'40% / 风险0.50%'};
const v2Percent=n=>n==null?'—':(n*100).toFixed(n!==0&&Math.abs(n)<.0001?4:2)+'%';
const v2Reasons={minimum_or_budget:'最低金额或预算',stop_distance:'止损距离',slots:'仓位上限',risk_pause:'组合暂停',cooldown:'退出冷却',no_averaging_down:'避免浮亏追加',signal_deviation:'信号价偏离',uncertain_order:'订单待确认',market_or_signal_stale:'行情或信号过期',exit_same_bar:'同根退出'};
function renderV2(d){lastV2=d;const v=d.v2;if(!v)return;const id=x=>document.getElementById(x),set=(x,t)=>id(x).textContent=t;
const phases={research_running:'研究运行中，尚未选择候选',no_qualified_candidate:'无候选通过研究门槛，V2 影子进程未启动',qualified_for_shadow:'已选出影子测试候选，盈利验收尚未完成',research_not_available:'尚无可用研究报告'};
set('v2Status',(phases[v.status]||v.status)+(v.code_matches?' · 代码版本匹配':' · 研究证据缺失或代码版本不匹配'));
const comparison=id('v2Comparison');comparison.replaceChildren();for(const[label,value]of[['V1 影子净值',num(d.stats.equity)],['V2 影子净值',num(v.shadow.stats?.equity)],['V2 状态',v.shadow.running?'心跳健康':v.shadow.status==='not_started'?'未启动':'心跳待检查'],['V2 独立累计',v.shadow.progress?(v.shadow.progress.elapsed_ms/86400000).toFixed(3)+' 天':'—']]){const cell=el('div');cell.append(el('small',label),el('b',value));comparison.append(cell);}
const body=id('v2Candidates');body.replaceChildren();for(const c of v.candidates){const m=c.stress[id('v2Slip').value];if(!m)continue;const n=c.name.lastIndexOf('-'),family=c.name.slice(0,n),budget=c.name.slice(n+1);const tr=el('tr');for(const value of[(v2Names[family]||family)+' · '+(v2Names[budget]||budget),v2Percent(m.net_return),pct(m.max_drawdown),m.trades,m.unique_signals,num(m.average_profit),m.max_positions,c.passed?'通过':'未通过'])tr.append(el('td',value));body.append(tr);}
if(!v.candidates.length){const tr=el('tr'),td=el('td','研究报告尚未完成');td.colSpan=8;tr.append(td);body.append(tr);}
const chooser=id('v2Candidate'),chosen=chooser.value;chooser.replaceChildren();for(const c of v.candidates){const n=c.name.lastIndexOf('-'),family=c.name.slice(0,n),budget=c.name.slice(n+1);const option=el('option',(v2Names[family]||family)+' · '+(v2Names[budget]||budget));option.value=c.name;chooser.append(option);}if(v.candidates.some(c=>c.name===chosen))chooser.value=chosen;
const selected=v.candidates.find(c=>c.name===chooser.value)?.stress[id('v2Slip').value];
function attribution(target,data){target.replaceChildren();if(!data){target.append(el('div','尚无归因数据。','empty'));return;}const wrap=el('div',null,'tablewrap'),table=el('table'),head=el('tr');for(const h of['归因维度','自然结束交易','已实现净收益 / 条'])head.append(el('th',h));table.append(head);for(const field of['symbol','mechanism'])for(const[name,item]of Object.entries(data[field]||{})){const tr=el('tr');for(const value of[v2Names[name]||name,item.trades,num(item.profit)])tr.append(el('td',value));table.append(tr);}wrap.append(table);target.append(wrap);}
attribution(id('v2Attribution'),selected?.attribution);set('v2HistoricalFilters','历史信号过滤：'+(Object.entries(selected?.filters||{}).map(([k,n])=>(v2Reasons[k]||k)+' '+n).join(' · ')||'暂无记录'));
attribution(id('v2LiveAttribution'),v.shadow.attribution);
set('v2Evidence','V2 工程测试：'+(v.drills.passed?v.drills.count+' 项通过':'证据待核实')+'；线上接口：'+(v.audit.available?'BTC / ETH 只读检查已完成':'尚未检查')+'；10 仓线上数量限制：'+(v.audit.online_limit_verified?'已验证':'未通过真实下单验证')+'。未来盈利验收要求规则冻结后至少 6 个月新数据与 30 笔完整交易。');
const risk=id('v2Risk');risk.replaceChildren();for(const[label,value]of[['活跃仓位',v.shadow.positions?.length||0],['投入占比',pct(v.shadow.stats?.allocation)],['保留风险额度',pct(v.shadow.stats?.reserved_risk)],['待确认订单',v.shadow.orders?.length||0]]){const cell=el('div');cell.append(el('small',label),el('b',value));risk.append(cell);}
const positions=id('v2Positions');positions.replaceChildren();if(!v.shadow.positions?.length)positions.append(el('div',v.shadow.status==='not_started'?'V2 尚未启动，暂无模拟持仓。':'当前无模拟持仓。','empty'));else{const wrap=el('div',null,'tablewrap'),table=el('table'),head=el('tr');for(const h of['币种 / 机制','投入 / 条','成交价','止损价','开仓时间'])head.append(el('th',h));table.append(head);for(const p of v.shadow.positions){const tr=el('tr');for(const value of[p.symbol+' / '+(v2Names[p.mechanism]||p.mechanism),num(p.stake/10000),num(p.entry),num(p.stop),stamp(p.opened)])tr.append(el('td',value));table.append(tr);}wrap.append(table);positions.append(wrap);}
set('v2Filters','V2 影子信号过滤：'+(Object.entries(v.shadow.filters||{}).map(([k,n])=>(v2Reasons[k]||k)+' '+n).join(' · ')||'暂无记录')+(v.correlation==null?'':'；BTC / ETH 小时收益相关系数 '+v.correlation.toFixed(3)));
}
document.getElementById('v2Slip').onchange=()=>{if(lastV2)renderV2(lastV2);};

document.getElementById('v2Candidate').onchange=()=>{if(lastV2)renderV2(lastV2);};
