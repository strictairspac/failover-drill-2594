/** @typedef {{id:number,name:string,role:'leader'|'follower',term:number,log_position:number,available:boolean,fenced:boolean,vote_enabled:boolean}} Node */
/** @typedef {{id:number,name:string,fail_primary:boolean,fence_primary:boolean,max_data_loss:number,required_term:number}} Scenario */
/** @typedef {{id:number,scenario_id:number,label:string,status:string,snapshot:object,result:object|null}} Drill */
export {};
/** @typedef {{id:number,drill_id:number,status:'pending'|'applying'|'applied'|'rejected',credential:string,evidence_sha256:string,actor:string,attempts:number,before_nodes:Node[]|null,after_nodes:Node[]|null,observed_nodes:Node[]|null,changes:Array<object>,problems:string[],applied_at:string|null}} DrillApply */
