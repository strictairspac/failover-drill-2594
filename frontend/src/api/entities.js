import {api,apiResult} from './client.js';
export const nodesAPI={list:()=>api('/nodes'),save:(data,id)=>api('/nodes'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify(data)})};
export const scenariosAPI={list:()=>api('/scenarios'),create:data=>api('/scenarios',{method:'POST',body:JSON.stringify(data)})};
export const drillsAPI={list:()=>api('/drills'),get:id=>api('/drills/'+id),create:data=>api('/drills',{method:'POST',body:JSON.stringify(data)})};
export const appliesAPI={
 list:()=>api('/applies'),
 get:drillId=>api('/drills/'+drillId+'/apply'),
 apply:drillId=>apiResult('/drills/'+drillId+'/apply',{method:'POST'}),
};
