"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const state = {generation:0,csrf:"",request:null,busy:false};
  const status = text => {$("enroll-status").textContent=text;};
  function clearReview() {state.request=null;$("enroll-review").hidden=true;$("enroll-details").replaceChildren();$("enroll-team").replaceChildren();}
  function clear(signedOut=false) {++state.generation;clearReview();$("enroll-code").value="";if(signedOut){state.csrf="";$("enroll-workspace").hidden=true;}}
  function controls(busy){state.busy=busy;for(const id of ["enroll-inspect-button","enroll-approve","enroll-deny"]){$(id).disabled=busy;}}
  async function api(path,body){
    const response=await fetch(path,{method:body===undefined?"GET":"POST",credentials:"same-origin",cache:"no-store",headers:{Accept:"application/json",...(body===undefined?{}:{"Content-Type":"application/json","X-CSRF-Token":state.csrf})},body:body===undefined?undefined:JSON.stringify(body)});
    const data=await response.json().catch(()=>null);
    if(!response.ok)throw new Error(`${response.status}: ${typeof data?.detail==="string"?data.detail.slice(0,300):"Request unavailable"}`);
    return data;
  }
  function detail(label,value){const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=label;dd.textContent=value;$("enroll-details").append(dt,dd);}
  $("enroll-inspect").onsubmit=async event=>{
    event.preventDefault();if(state.busy)return;
    const code=$("enroll-code").value.trim();
    if(!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(code)){status("Enter the complete UUID approval code from your relay.");return;}
    const generation=++state.generation;clearReview();controls(true);
    try{const result=await api("/auth/agent/enrollment/inspect",{request_id:code});if(generation!==state.generation)return;
      if(result.state!=="pending"){status(`Request is ${result.state}. Nothing to approve.`);return;}
      state.request={id:result.request_id,expires:Date.parse(result.expires_at)};
      detail("Device label (unverified)",result.device_name);detail("Approval code",result.request_id);detail("Request expires",result.expires_at);detail("Permissions","inference only");detail("Allowed models",result.policy.models.join(", "));detail("Key lifetime",`${result.policy.expires_in_days} days`);detail("Rate limits",`${result.policy.rpm} requests/minute; ${result.policy.tpm} tokens/minute`);
      $("enroll-team").replaceChildren(...result.teams.map(team=>{const option=document.createElement("option");option.value=team.id;option.textContent=team.name;return option;}));
      $("enroll-review").hidden=false;status(result.teams.length?"Review the code, device and limits, then explicitly approve or deny.":"No active eligible teams. You may deny this request.");
    }catch(error){if(generation===state.generation){clearReview();status(error.message);}}
    finally{controls(false);$("enroll-approve").disabled=!state.request||!$("enroll-team").value;}
  };
  async function decide(action){
    if(state.busy||!state.request)return;
    if(!Number.isFinite(state.request.expires)||state.request.expires<=Date.now()){clearReview();status("Request expired. Start a new request on your relay.");return;}
    const team=$("enroll-team").value;if(action==="approve"&&!team)return;
    const generation=state.generation,id=state.request.id;controls(true);
    try{await api(`/auth/agent/enrollment/${action}`,{request_id:id,...(action==="approve"?{team_id:team}:{})});if(generation!==state.generation)return;clear();status(action==="approve"?"Relay approved. Return to your relay to finish enrollment.":"Request denied.");}
    catch(error){if(generation===state.generation){clearReview();status(error.message+" Review the request again before retrying.");}}
    finally{controls(false);}
  }
  $("enroll-approve").onclick=()=>decide("approve");$("enroll-deny").onclick=()=>decide("deny");
  $("enroll-code").oninput=()=>{++state.generation;clearReview();};
  $("enroll-clear").onclick=()=>{clear();status("Cleared locally. A pending request remains pending until denied or expired.");};
  $("enroll-logout").onclick=()=>{const pending=api("/auth/logout",{}).catch(()=>{});clear(true);status("Signed out locally. If the server was unavailable, your SSO session may remain active.");void pending;};
  window.addEventListener("pagehide",()=>clear(true));
  const generation=state.generation;
  api("/auth/session").then(session=>{if(generation!==state.generation)return;if(!session.authenticated||!session.csrf_token){status("Sign in with SSO, then return here to review your relay code.");return;}state.csrf=session.csrf_token;$("enroll-workspace").hidden=false;status("Enter the approval code shown by your own relay.");}).catch(()=>{if(generation===state.generation)status("Unable to verify SSO session. Sign in and reload this page.");});
})();
