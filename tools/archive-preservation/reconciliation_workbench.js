"use strict";
const $=id=>document.getElementById(id);
function el(tag,attrs={},...children){const n=document.createElement(tag);for(const [key,value] of Object.entries(attrs)){if(key.startsWith("on"))n.addEventListener(key.slice(2),value);else if(value!==false&&value!==undefined)n.setAttribute(key,String(value));}for(const child of children.flat()){if(child!==null&&child!==undefined)n.append(child instanceof Node?child:document.createTextNode(String(child)));}return n;}
let overview,current,draft,dirty=false,saving=false,loadTicket=0,mode="side",linked=true,opacity=.5;
let panes=[],combined,active=0;
const bytes=n=>n<1048576?`${(n/1024).toFixed(1)} KB`:`${(n/1048576).toFixed(1)} MB`;
function error(e){$("error").hidden=false;$("error").textContent=e.message||String(e);}
async function api(path,options){const response=await fetch(path,options);let result;try{result=await response.json();}catch{throw Error("The workbench returned an unreadable response.");}if(!response.ok)throw Error(result.error||"Request failed");return result;}
function pending(o){return !o.selected||o.metadata_pending.length>0;}
function visible(){const q=$("search").value.trim().toLowerCase(),f=$("filter").value;return overview.objects.filter(o=>(!q||[o.id,o.inventory,o.title,o.note].join(" ").toLowerCase().includes(q))&&(f==="all"||f==="pending"&&pending(o)||f==="master"&&!o.selected||f==="metadata"&&o.metadata_pending.length||f==="complete"&&!pending(o))).sort((a,b)=>Number(pending(b))-Number(pending(a)));}
function renderQueue(){const records=visible();$("counts").textContent=`${records.length} shown · ${overview.objects.filter(pending).length} need review · ${overview.objects.length} total`;$("queue").replaceChildren(...records.map(o=>el("button",{class:"queue-item","aria-current":String(current?.object.id===o.id),onclick:()=>navigate(o.id)},el("strong",{},o.inventory||o.id),o.title,el("small",{},!o.selected?"Master unresolved":o.metadata_pending.length?"Master selected · metadata pending":"Review complete"))));if(!records.length)$("queue").append(el("p",{class:"muted"},"No objects match this filter."));}
function confirmLeave(){return !dirty||confirm("Discard the unsaved choices on this object?");}
async function refresh(ask=true){if(saving||ask&&!confirmLeave())return;try{overview=await api("/api/overview");$("unit-title").textContent=overview.title;$("unit-status").textContent=`${overview.read_only?"Read-only unit":"Review work area"} · ${overview.actor}`;renderQueue();const requested=new URLSearchParams(location.search).get("object");const id=current?.object.id||requested||visible()[0]?.id||overview.objects[0]?.id;if(id)await navigate(id,false);else $("review").replaceChildren(el("p",{class:"empty"},"No prepared objects."));}catch(e){error(e);}}
async function navigate(id,ask=true){if(saving||ask&&!confirmLeave())return;const ticket=++loadTicket;try{const result=await api("/api/object?id="+encodeURIComponent(id));if(ticket!==loadTicket)return;current=result;draft=structuredClone(result.decision);dirty=false;$("error").hidden=true;history.replaceState(null,"","?"+new URLSearchParams({object:id}));renderObject();renderQueue();}catch(e){error(e);}}
function changed(){dirty=true;$("save-status").textContent="Unsaved choices";$("save-status").className="";for(const card of document.querySelectorAll(".candidate"))card.classList.toggle("chosen",card.dataset.candidate===draft.selected_candidate_id);}
function eligible(c){return c.eligible&&c.complete&&!["derivative","reference","component","excluded"].includes(c.role);}
function selectMaster(id){draft.selected_candidate_id=id;changed();}
function renderObject(){const o=current.object,readOnly=current.read_only;mode="side";linked=true;opacity=.5;panes=[];active=0;
 const nav=el("div",{class:"toolbar"}),order=overview.objects,idx=order.findIndex(x=>x.id===o.id);nav.append(el("button",{disabled:idx===0,onclick:()=>navigate(order[idx-1].id)},"← Previous"),el("button",{disabled:idx===order.length-1,onclick:()=>navigate(order[idx+1].id)},"Next →"));
 const root=$("review");root.replaceChildren(el("div",{class:"heading"},el("div",{},el("span",{class:"eyebrow"},o.inventory||o.id),el("h2",{},o.title||o.id),el("p",{class:"muted"},o.provenance||"Local evidence · no external catalogue link supplied")),nav));
 if(readOnly)root.append(el("p",{class:"notice"},"This unit is completed or read-only. Earlier choices are retained; editing is disabled."));
 if(!overview.projection_agrees)root.append(el("p",{class:"notice"},"The derived disposition CSV is absent or out of date. Readable decisions remain authoritative; the next successful save rebuilds it."));
 const status=el("div",{},el("span",{class:"badge"},draft.selected_candidate_id?"One Master selected":"Master unresolved"),el("span",{class:"badge"},`${o.candidates.length} candidate representations`));root.append(status);
 const toolbar=el("div",{class:"toolbar"});for(const [key,label] of [["side","Side by side"],["overlay","Overlay"],["difference","Difference"]])toolbar.append(el("button",{"data-mode":key,"aria-pressed":String(key==="side"),onclick:()=>setMode(key)},label));
 toolbar.append(el("span",{class:"spacer"}),el("label",{},el("input",{type:"checkbox",checked:true,onchange:e=>{linked=e.target.checked;}}),"Link views"),el("button",{onclick:()=>changeZoom(1/1.4),"aria-label":"Zoom out"},"−"),el("button",{onclick:()=>changeZoom(1.4),"aria-label":"Zoom in"},"+"),el("button",{onclick:resetView},"Fit"),el("button",{onclick:()=>panes.forEach((_,i)=>loadImage(i,4000))},"Load detail"));root.append(toolbar);
 const comparison=el("div",{class:"compare",id:"comparison"});root.append(comparison);o.candidates.length?comparison.append(makePane(0),makePane(1)):comparison.append(el("p",{class:"empty"},"No candidates were prepared. Leave this object unresolved."));
 combined=el("canvas",{"aria-label":"Combined image comparison"});bindCanvas(combined,2);root.append(el("div",{class:"stage combined",id:"combined",hidden:true},combined));
 root.append(el("label",{id:"opacity-label",hidden:true},"Overlay amount ",el("input",{type:"range",min:0,max:100,value:50,oninput:e=>{opacity=Number(e.target.value)/100;drawAll();}})),el("p",{class:"muted"},"Wheel to zoom; drag to pan. Unlink views to align different crops. Rendered differences reflect framing and conversion too; they do not establish file equality."));
 const textButton=el("button",{onclick:showText},"Compare PDF page text");root.append(el("details",{},el("summary",{},"PDF text evidence"),el("p",{class:"muted"},"Extracts the displayed pages’ embedded text; it does not run OCR."),textButton,el("div",{id:"text-result",class:"text-result"})));
 const choices=el("section",{class:"panel"},el("h3",{},"Master representation"),el("p",{class:"muted"},"Select one complete representation. Other candidates become redundant; source files remain unchanged."));
 const unresolved=el("input",{type:"radio",name:"master",value:"",checked:!draft.selected_candidate_id,disabled:readOnly,onchange:()=>selectMaster("")});choices.append(el("label",{},unresolved," Leave unresolved"));
 for(const group of candidateGroups(o.candidates)){
  const representative=group.find(c=>c.id===draft.selected_candidate_id)||group[0];
  const others=group.filter(c=>c!==representative);
  choices.append(candidateCard(representative,readOnly));
  if(others.length){
   const copies=el("details",{},el("summary",{},`${others.length} additional exact-copy occurrence(s) · original paths retained`));
   others.forEach(c=>copies.append(candidateCard(c,readOnly)));
   choices.append(copies);
  }
 }
 root.append(choices);
 const metadata=el("section",{class:"panel"},el("h3",{},"Metadata evidence"),el("p",{class:"muted"},"Metadata choices are separate from the Master. Retain a supported value or leave its conflict unresolved."));if(!o.metadata.length)metadata.append(el("p",{class:"muted"},"No metadata alternatives were prepared for this object."));for(const field of o.metadata){const box=el("div",{class:"metadata-row"},el("strong",{},field.field));const name="metadata-"+field.field;box.append(el("label",{},el("input",{type:"radio",name,checked:!draft.metadata_choices[field.field],disabled:readOnly,onchange:()=>{delete draft.metadata_choices[field.field];changed();}})," Leave unresolved"));for(const c of field.choices)box.append(el("label",{},el("input",{type:"radio",name,checked:draft.metadata_choices[field.field]===c.id,disabled:readOnly,onchange:()=>{draft.metadata_choices[field.field]=c.id;changed();}}),` ${c.value}`,el("span",{class:"muted"},` · ${c.source||"Prepared evidence"}`)));metadata.append(box);}root.append(metadata);
 const note=el("textarea",{id:"note",maxlength:2000,disabled:readOnly,oninput:e=>{draft.note=e.target.value;changed();}});note.value=draft.note;root.append(el("section",{class:"panel"},el("label",{for:"note"},"Short review note"),note));
 root.append(el("section",{class:"panel decision-bar"},el("p",{},el("strong",{id:"save-status",role:"status",class:"good"},draft.decided_at?`Saved by ${draft.actor} · ${new Date(draft.decided_at).toLocaleString()}`:"No new decision saved")),el("button",{id:"save",class:"primary",disabled:readOnly,onclick:save},"Save choices")));
 if(o.candidates.length){const first=o.candidates.find(c=>c.id===draft.selected_candidate_id)||o.candidates[0];choosePane(0,first.id);choosePane(1,(o.candidates.find(c=>c.id!==first.id)||first).id);}requestAnimationFrame(drawAll);
}
function candidateGroups(candidates){
 const groups=new Map();
 for(const c of candidates){
  const identity=c.members.map(m=>m.sha256?`sha256:${m.sha256}`:`path:${m.path}:${m.size_bytes}:${m.mtime_ns}`);
  const key=JSON.stringify([c.eligible,c.complete,c.role,identity]);
  if(!groups.has(key))groups.set(key,[]);
  groups.get(key).push(c);
 }
 return [...groups.values()];
}
function candidateCard(c,readOnly){
 const chosen=draft.selected_candidate_id===c.id;
 const radio=el("input",{type:"radio",name:"master",value:c.id,checked:chosen,disabled:readOnly||!eligible(c)||!c.available,onchange:()=>selectMaster(c.id)});
 const card=el("article",{class:"candidate"+(chosen?" chosen":""),"data-candidate":c.id},
  el("label",{},radio,el("span",{},c.label||c.id),el("span",{class:"badge"},eligible(c)?`${c.members.length} member(s)`:`Comparison only · ${c.role}`)),
  el("p",{class:"muted"},c.evidence||"No additional comparison evidence supplied."));
 if(!c.available)card.append(el("p",{class:"notice"},c.unavailable_reason));
 const buttons=el("div",{class:"choices"});
 for(let i=0;i<2;i++)buttons.append(el("button",{onclick:()=>choosePane(i,c.id)},`Compare in ${i?"B":"A"}`));
 card.append(buttons,el("details",{},el("summary",{},"Ordered members and provenance"),memberTable(c)));
 return card;
}
function memberTable(c){return el("table",{},el("thead",{},el("tr",{},["Order / path","Recorded technical evidence"].map(x=>el("th",{},x)))),el("tbody",{},c.members.map((m,i)=>el("tr",{},el("td",{class:"path"},`${i+1}. ${m.path}`),el("td",{},`${bytes(m.size_bytes)} · ${m.pages} page(s)`,el("p",{class:"path"},m.sha256?`Stored SHA-256: ${m.sha256}`:"No stored cryptographic hash supplied"))))));}
function makePane(i){const p={cid:"",member:0,page:1,edge:1200,image:null,ticket:0,zoom:1,x:0,y:0};panes[i]=p;const select=el("select",{"aria-label":`Candidate ${i?"B":"A"}`,onchange:e=>choosePane(i,e.target.value)},current.object.candidates.map(c=>el("option",{value:c.id},c.label||c.id)));p.select=select;p.memberSelect=el("select",{"aria-label":`Member ${i?"B":"A"}`,onchange:e=>{p.member=Number(e.target.value);p.page=1;updatePages(i);loadImage(i);}});p.pageInput=el("input",{type:"number",min:1,value:1,"aria-label":`Page ${i?"B":"A"}`,onchange:e=>{p.page=Math.max(1,Math.min(Number(e.target.value)||1,current.object.candidates.find(c=>c.id===p.cid).members[p.member].pages));updatePages(i);loadImage(i);}});p.status=el("p",{class:"muted",role:"status"});p.canvas=el("canvas",{"aria-label":`Image ${i?"B":"A"}`});bindCanvas(p.canvas,i);return el("section",{class:"pane"},el("h3",{},i?"B · Alternative / reference":"A · Current Master / candidate"),select,el("div",{class:"pages"},p.memberSelect,el("label",{},"Page ",p.pageInput)),el("div",{class:"stage"},p.canvas),p.status);}
function choosePane(i,cid){const p=panes[i];if(!p)return;p.cid=cid;p.select.value=cid;p.member=0;p.page=1;p.zoom=1;p.x=p.y=0;const c=current.object.candidates.find(c=>c.id===cid);p.memberSelect.replaceChildren(...c.members.map((m,j)=>el("option",{value:j},`${j+1}. ${m.path.split("/").pop()}`)));updatePages(i);loadImage(i);}
function updatePages(i){const p=panes[i],m=current.object.candidates.find(c=>c.id===p.cid).members[p.member];p.pageInput.value=p.page;p.pageInput.max=m.pages;p.pageInput.disabled=m.pages===1;$("text-result")?.replaceChildren();}
function mediaURL(p,route="/media"){return route+"?"+new URLSearchParams({candidate:p.cid,member:p.member,page:p.page,edge:p.edge});}
async function loadImage(i,edge=1200){const p=panes[i];p.edge=edge;p.image=null;const ticket=++p.ticket,objectID=current.object.id;p.status.textContent="Loading preview…";drawAll();const img=new Image();img.onload=()=>{if(ticket!==p.ticket||current.object.id!==objectID)return;p.image=img;p.status.textContent=`${img.naturalWidth} × ${img.naturalHeight} preview pixels · page ${p.page} · ${edge===4000?"detail":"standard"} view`;drawAll();};img.onerror=async()=>{if(ticket!==p.ticket||current.object.id!==objectID)return;p.status.textContent="Preview unavailable. Check the source evidence and mount.";try{const response=await fetch(mediaURL(p));if(!response.ok){const data=await response.json();if(ticket===p.ticket&&current.object.id===objectID)p.status.textContent=data.error;}}catch{}drawAll();};img.src=mediaURL(p);}
function setMode(value){mode=value;document.querySelectorAll("[data-mode]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.mode===value)));$("combined").hidden=value==="side";$("comparison").querySelectorAll(".stage").forEach(s=>s.hidden=value!=="side");$("opacity-label").hidden=value!=="overlay";drawAll();}
function resetView(){for(const p of panes){p.zoom=1;p.x=p.y=0;}drawAll();}
function changeZoom(factor){for(const i of linked?[0,1]:[active]){const p=panes[i];if(p)p.zoom=Math.min(12,Math.max(.5,p.zoom*factor));}drawAll();}
function bindCanvas(canvas,index){let drag=null;canvas.addEventListener("wheel",e=>{e.preventDefault();if(index<2)active=index;changeZoom(e.deltaY<0?1.15:1/1.15);},{passive:false});canvas.addEventListener("pointerdown",e=>{if(index<2)active=index;drag={x:e.clientX,y:e.clientY};canvas.setPointerCapture(e.pointerId);});canvas.addEventListener("pointermove",e=>{if(!drag)return;for(const i of linked?[0,1]:[active]){const p=panes[i];p.x+=(e.clientX-drag.x)/canvas.clientWidth;p.y+=(e.clientY-drag.y)/canvas.clientHeight;}drag={x:e.clientX,y:e.clientY};drawAll();});for(const event of ["pointerup","pointercancel"])canvas.addEventListener(event,()=>drag=null);canvas.addEventListener("dblclick",resetView);}
function draw(canvas,index){if(!canvas||!canvas.clientWidth||!canvas.clientHeight)return;const ratio=Math.min(devicePixelRatio||1,2),w=canvas.width=Math.round(canvas.clientWidth*ratio),h=canvas.height=Math.round(canvas.clientHeight*ratio),ctx=canvas.getContext("2d");ctx.fillStyle="#e1e5dd";ctx.fillRect(0,0,w,h);const paint=(i,alpha=1,difference=false)=>{const p=panes[i];if(!p?.image)return;const im=p.image,scale=Math.min(w/im.naturalWidth,h/im.naturalHeight)*p.zoom,iw=im.naturalWidth*scale,ih=im.naturalHeight*scale;ctx.globalAlpha=alpha;ctx.globalCompositeOperation=difference?"difference":"source-over";ctx.drawImage(im,(w-iw)/2+p.x*w,(h-ih)/2+p.y*h,iw,ih);};if(index<2)paint(index);else{paint(0);paint(1,mode==="overlay"?opacity:1,mode==="difference");}ctx.globalAlpha=1;ctx.globalCompositeOperation="source-over";}
function drawAll(){panes.forEach((p,i)=>draw(p.canvas,i));if(mode!=="side")draw(combined,2);}
async function showText(){const target=$("text-result"),signature=panes.map(p=>mediaURL(p,"/text")).join("|");target.replaceChildren(el("p",{},"Extracting embedded page text…"));try{const texts=await Promise.all(panes.map(async p=>{const response=await fetch(mediaURL(p,"/text"));if(!response.ok){const result=await response.json();throw Error(result.error);}return response.text();}));if(signature!==panes.map(p=>mediaURL(p,"/text")).join("|"))return;target.replaceChildren(...texts.map((t,i)=>el("div",{},el("strong",{},i?"B · Page text":"A · Page text"),el("pre",{},t||"No embedded text on this page."))));}catch(e){target.replaceChildren(el("p",{class:"notice"},e.message));}}
async function save(){
 if(saving)return;
 saving=true;
 const controls=[...document.querySelectorAll('#review input,#review select,#review textarea,#review button')].map(n=>[n,n.disabled]);
 controls.forEach(([n])=>n.disabled=true);
 $("save-status").textContent="Saving…";
 try{
  const result=await api("/api/decision",{method:"POST",headers:{"Content-Type":"application/json","X-Review-Token":overview.token},body:JSON.stringify({object_id:current.object.id,selected_candidate_id:draft.selected_candidate_id,metadata_choices:draft.metadata_choices,note:draft.note,revision:current.revision})});
  current=result;draft=structuredClone(result.decision);dirty=false;
  overview=await api("/api/overview");renderObject();renderQueue();$("error").hidden=true;
 }catch(e){
  error(e);
  $("save-status").textContent=e.message.startsWith("Choices were saved")?"Choices saved; reload to inspect the projection warning.":"Save could not be confirmed. Your choices are still on screen; reload before retrying.";
 }finally{
  controls.forEach(([n,disabled])=>{if(n.isConnected)n.disabled=disabled;});
  saving=false;
 }
}
$("search").addEventListener("input",()=>overview&&renderQueue());$("filter").addEventListener("change",()=>overview&&renderQueue());$("reload").addEventListener("click",()=>refresh());window.addEventListener("resize",drawAll);window.addEventListener("beforeunload",e=>{if(dirty){e.preventDefault();e.returnValue="";}});refresh(false);
