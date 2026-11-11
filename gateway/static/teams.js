"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const state = {bearer:"",csrf:"",generation:0,timer:null,busy:false};
  const status = message => { $("team-status").textContent=message; };
  function clearSecret() { clearTimeout(state.timer); $("team-secret").value=""; if ($("team-secret-dialog").open) $("team-secret-dialog").close(); }
  function clear() { $("team-member-form").reset(); $("team-key-form").reset(); ++state.generation; state.bearer=""; state.csrf=""; clearSecret(); $("team-token").value=""; $("team-workspace").hidden=true; $("team-select").replaceChildren(); $("team-members").replaceChildren(); $("team-keys").replaceChildren(); }
  async function api(path,method="GET",body) {
    const headers={Accept:"application/json"};
    if(state.bearer) headers.Authorization=`Bearer ${state.bearer}`;
    if(body!==undefined) headers["Content-Type"]="application/json";
    if(method!=="GET" && state.csrf) headers["X-CSRF-Token"]=state.csrf;
    const response=await fetch(path,{method,headers,credentials:"same-origin",cache:"no-store",body:body===undefined?undefined:JSON.stringify(body)});
    const data=await response.json().catch(()=>null);
    if(!response.ok) throw new Error(`${response.status}: ${typeof data?.detail==="string"?data.detail.slice(0,500):"Request rejected"}`);
    return data;
  }
  function cell(row,value){ const td=document.createElement("td");td.textContent=String(value??"");row.append(td);return td; }
  function button(label,action){const b=document.createElement("button");b.type="button";b.textContent=label;b.onclick=()=>action(b);return b;}
  function table(headers,rows,build){const t=document.createElement("table"),head=document.createElement("tr");for(const h of headers){const th=document.createElement("th");th.textContent=h;th.scope="col";head.append(th);}t.append(head);for(const row of rows){const tr=document.createElement("tr");build(tr,row);t.append(tr);}return t;}
  const path=()=>`/teams/api/${encodeURIComponent($("team-select").value)}`;
  async function selected(generation=++state.generation){
    clearSecret();status("Loading team…");$("team-members").replaceChildren();$("team-keys").replaceChildren();if(!$("team-select").value)return;
    const base=path();
    try{
      const [members,keys]=await Promise.all([api(`${base}/members`),api(`${base}/keys`)]);
      if(generation!==state.generation)return;
      $("team-members").replaceChildren(table(["User","Role","Source","Action"],members,(tr,m)=>{
        cell(tr,m.user_id);cell(tr,m.role);cell(tr,m.primary?"Primary":"Explicit");const c=cell(tr,"");
        if(!m.primary)c.append(button("Remove membership",b=>mutate(b,`${base}/members/${encodeURIComponent(m.user_id)}`,"DELETE")));
      }));
      $("team-keys").replaceChildren(table(["Name","User","Expires","State","Actions"],keys,(tr,k)=>{
        cell(tr,k.name);cell(tr,k.user_id);cell(tr,k.expires_at);const expired=Date.parse(k.expires_at)<=Date.now();cell(tr,k.revoked?"Revoked":expired?"Expired":"Active");const c=cell(tr,"");
        if(!k.revoked&&!expired){c.append(button("Rotate",b=>mutate(b,`${base}/keys/${encodeURIComponent(k.id)}/rotate`,"POST")));c.append(button("Revoke",b=>mutate(b,`${base}/keys/${encodeURIComponent(k.id)}/revoke`,"POST")));}
      }));status("Team loaded.");
    }catch(error){if(generation===state.generation)status(error.message);}
  }
  async function mutate(control,url,method,body){
    if(method==="DELETE"&&!confirm("Remove this membership and revoke its team keys?"))return;
    if(url.endsWith("/revoke")&&!confirm("Revoke this key immediately?"))return;
    if(url.endsWith("/rotate")&&!confirm("Rotate this key? The old credential stops working immediately."))return;
    if(state.busy){status("A change is still pending. Wait before making another.");return;}state.busy=true;const generation=state.generation;if(control)control.disabled=true;clearSecret();
    try{const result=await api(url,method,body);if(generation!==state.generation)return;await selected(generation);if(generation!==state.generation)return;
      status("Change saved.");if(result?.key){$("team-secret").value=result.key;$("team-secret-dialog").showModal();state.timer=setTimeout(clearSecret,120000);}
    }catch(error){if(generation===state.generation)status(error.message);}finally{state.busy=false;if(control)control.disabled=false;}
  }
  async function connect(generation){
    try{const teams=await api("/teams/api");if(generation!==state.generation)return;
      $("team-select").replaceChildren(...teams.map(t=>{const option=document.createElement("option");option.value=t.id;option.textContent=t.name;return option;}));
      $("team-workspace").hidden=false;if(teams.length)await selected(generation);else status("No teams delegated to this credential or session.");
    }catch(error){if(generation===state.generation)status(error.message);}
  }
  $("team-login").onsubmit=event=>{event.preventDefault();const token=$("team-token").value;clear();state.bearer=token;connect(state.generation);};
  $("team-select").onchange=()=>{$("team-member-form").reset();$("team-key-form").reset();selected();};$("team-refresh").onclick=()=>selected();
  $("team-member-form").onsubmit=event=>{event.preventDefault();mutate(event.submitter,`${path()}/members/${encodeURIComponent($("member-id").value)}`,"PUT",{role:$("member-role").value});};
  $("team-key-form").onsubmit=event=>{event.preventDefault();try{const models=JSON.parse($("key-models").value);if(!Array.isArray(models)||models.some(m=>typeof m!=="string"))throw new Error("Models must be a JSON string array");mutate(event.submitter,`${path()}/keys`,"POST",{name:$("key-name").value,user_id:$("key-user").value,team_id:$("team-select").value,kind:$("key-kind").value,allowed_models:models,permissions:["inference"],expires_in_days:Number($("key-days").value),rpm:Number($("key-rpm").value),tpm:Number($("key-tpm").value)});}catch(error){status(error.message);}};
  $("team-logout").onclick=()=>{const pending=api("/auth/logout","POST").catch(()=>{});clear();status("Signed out locally. If the server was unavailable, your SSO session may remain active.");void pending;};
  $("team-secret-close").onclick=clearSecret;$("team-secret-dialog").onclose=clearSecret;$("team-secret-dialog").oncancel=clearSecret;window.addEventListener("pagehide",clear);
  const generation=state.generation;api("/auth/session").then(session=>{if(generation!==state.generation)return;state.csrf=session.csrf_token||"";return connect(generation);}).catch(()=>{if(generation===state.generation)status("Sign in to manage delegated teams.");});
})();
