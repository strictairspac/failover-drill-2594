import {nodesAPI,scenariosAPI,drillsAPI,appliesAPI} from '../api/entities.js';
export const store={nodes:[],scenarios:[],drills:[],applies:[],async refresh(){[this.nodes,this.scenarios,this.drills,this.applies]=await Promise.all([nodesAPI.list(),scenariosAPI.list(),drillsAPI.list(),appliesAPI.list()]);}};
