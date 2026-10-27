(function () {
  'use strict';
  let state=JSON.parse(document.getElementById('studio-data').textContent), config=state.config, selected=-1, dirty=false, busy=false;
  const fields=state.fields, sample={}, byId=id=>fields.find(f=>f.id===id), $=id=>document.getElementById(id);
  function el(tag,text,cls) { const node=document.createElement(tag); if(text!==undefined) node.textContent=text; if(cls) node.className=cls; return node; }
  function button(text,action) { const b=el('button',text,'btn sm'); b.type='button'; b.addEventListener('click',action); return b; }
  function option(select,value,label) { const o=el('option',label); o.value=value; select.append(o); }
  function label(item) { return item.label_override || byId(item.field_id)?.label || state.relations.find(r=>r.id===item.relation_id)?.name || 'Section'; }
  function settings() { dirty=true; state.tested=false; $('draft-status').textContent='Unsaved draft changes. The live form is unchanged.'; updateButtons(); renderCanvas(); }
  function updateButtons() {
    document.querySelectorAll('.studio-settings,.studio-layout,#studio-preview').forEach(node=>node.inert=busy);
    ['draft-save','preview-run','draft-publish','draft-reset','draft-restore'].forEach(id=>$(id).disabled=busy);
    $('preview-run').disabled=busy||dirty;
    $('draft-publish').disabled=busy||dirty||!state.tested||state.stale||state.issues.some(i=>i.level==='error');
    $('draft-restore').disabled=busy||!state.revisions.length;
  }
  async function request(action,extra={}) {
    if(busy) return; busy=true; updateButtons();
    try {
      const response=await fetch(location.pathname+'/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':document.querySelector('[name=studio-csrf]').content},body:JSON.stringify({version:state.version,...extra})});
      const result=await response.json(); if(!response.ok) throw new Error(result.error||'Request failed');
      state={...state,...result}; config=state.config; dirty=false;
      $('draft-status').textContent=action==='publish'?'Published. The live form now uses this configuration.':action==='preview'?'Preview completed. No records were created.':'Draft saved.';
      render(); if(result.preview) renderPreview(result.preview);
    } catch(error) { $('draft-status').textContent=error.message; }
    finally {busy=false;updateButtons();}
  }
  function add(item) {config.items.push({kind:'field',field_id:null,relation_id:null,label_override:null,help_text:null,required:false,readonly:false,parent_field_id:null,filter_field_id:null,rules_json:null,widget:null,...item});selected=config.items.length-1;settings();renderPalette();renderEditor();}
  function move(from,to) {if(to<0||to>=config.items.length)return;const [item]=config.items.splice(from,1);config.items.splice(to,0,item);selected=to;settings();renderEditor();}
  function renderPalette() {
    $('studio-palette').replaceChildren(); fields.forEach(f=>{const b=button(`${f.label} · ${f.type}`,()=>add({field_id:f.id}));b.disabled=config.items.some(i=>i.field_id===f.id);$('studio-palette').append(b);});
    $('studio-relations').replaceChildren();state.relations.forEach(r=>{const b=button(r.name,()=>add({kind:'relation',relation_id:r.id}));b.disabled=config.items.some(i=>i.relation_id===r.id);$('studio-relations').append(b);});
  }
  function renderCanvas() {
    const canvas=$('studio-canvas');canvas.replaceChildren();
    config.items.forEach((item,index)=>{
      const card=el('div',undefined,'studio-control'+(selected===index?' selected':''));card.draggable=true;
      card.addEventListener('dragstart',e=>e.dataTransfer.setData('text/plain',String(index)));
      card.addEventListener('dragover',e=>e.preventDefault());card.addEventListener('drop',e=>{e.preventDefault();const from=Number(e.dataTransfer.getData('text/plain'));if(Number.isInteger(from)&&from>=0&&from<config.items.length)move(from,index);});
      const select=button(label(item),()=>{selected=index;renderCanvas();renderEditor();});select.className='studio-control-label';card.append(select);
      const tags=[];if(item.required)tags.push('Required');if(item.readonly)tags.push('Read-only');if(item.rules_json)tags.push('Conditional');if(item.parent_field_id)tags.push('Dependent');if(item.kind==='relation'||byId(item.field_id)?.type==='relation')tags.push('Related');
      const tagLine=el('div',tags.join(' · '),'muted');card.append(tagLine);
      if(item.kind!=='section'){const mock=el('div',byId(item.field_id)?.type==='enum'?'Choose an option…':item.kind==='relation'?'Choose related records…':'Enter '+label(item).toLowerCase()+'…','studio-mock');card.append(mock);}
      const controls=el('div',undefined,'toolbar');controls.append(button('Move up',()=>move(index,index-1)),button('Move down',()=>move(index,index+1)),button('Remove',()=>{config.items.splice(index,1);selected=-1;settings();renderPalette();renderEditor();}));card.append(controls);canvas.append(card);
    });
    if(!config.items.length)canvas.append(el('p','Add a field or section to begin.','muted'));
  }
  function inputSetting(parent,title,item,key,type='text') {
    const wrap=el('label',title), input=el('input');input.type=type;
    if(type==='checkbox')input.checked=!!item[key];else input.value=item[key]||'';
    input.addEventListener('input',()=>{item[key]=type==='checkbox'?input.checked:input.value;settings();});wrap.append(input);parent.append(wrap);
  }
  function renderEditor() {
    const editor=$('studio-editor');editor.replaceChildren();const item=config.items[selected];if(!item){editor.append(el('p','Select a control.','muted'));return;}
    inputSetting(editor,'Label',item,'label_override');inputSetting(editor,'Help text',item,'help_text');
    if(item.kind==='section')return;
    inputSetting(editor,'Required',item,'required','checkbox');inputSetting(editor,'Read-only',item,'readonly','checkbox');
    const field=byId(item.field_id);
    if(field?.type==='relation') {
      const parent=el('select');parent.setAttribute('aria-label','Controlling field');option(parent,'','No dependency');
      fields.filter(f=>f.type==='relation'&&f.id!==field.id&&config.items.some(i=>i.field_id===f.id)).forEach(f=>option(parent,f.id,f.label));parent.value=item.parent_field_id||'';
      editor.append(el('h3','Dependent dropdown'),el('p','Show only choices belonging to the selected parent. Test with sample values below.','muted'),el('label','Filter by'),parent);
      parent.addEventListener('change',()=>{item.parent_field_id=Number(parent.value)||null;item.filter_field_id=null;settings();renderEditor();});
      const match=el('select');match.setAttribute('aria-label','Match field');option(match,'','Automatic match');const target=byId(item.parent_field_id)?.related_table_id;
      (state.related_fields[String(field.id)]||[]).filter(f=>f.related_table_id===target).forEach(f=>option(match,f.id,f.label));match.value=item.filter_field_id||'';match.addEventListener('change',()=>{item.filter_field_id=Number(match.value)||null;settings();});editor.append(el('label','Match on'),match);
    }
    if(!field||['file','image','autonumber','formula'].includes(field.type))return;
    editor.append(el('h3','Conditional behavior'),el('p','Hidden values are preserved. These rules do not grant access to fields.','muted'));
    const rules=JSON.parse(item.rules_json||'{}');const save=()=>{item.rules_json=Object.keys(rules).length?JSON.stringify(rules):null;settings();};
    ['show','require','readonly'].forEach(action=>{
      const block=el('section',undefined,'rule-block');block.append(el('h4',{show:'Show when',require:'Require when',readonly:'Read-only when'}[action]));
      if(rules[action]) {drawRule(block,rules[action],()=>{delete rules[action];save();renderEditor();},save,field);}
      else block.append(button('Add condition',()=>{const first=fields.find(f=>f.id!==field.id&&config.items.some(i=>i.field_id===f.id)&&!['file','image','formula','autonumber','json','tags'].includes(f.type));if(!first)return;rules[action]={all:[{field:first.name,op:'eq',value:''}]};save();renderEditor();}));editor.append(block);
    });
  }
  function drawRule(parent,node,remove,save,target) {
    const box=el('div',undefined,'rule-node');parent.append(box);
    const choices=fields.filter(f=>f.id!==target.id&&config.items.some(i=>i.field_id===f.id)&&!['file','image','formula','autonumber','json','tags'].includes(f.type));
    if(node.all||node.any) {
      let key=node.all?'all':'any';const select=el('select');select.setAttribute('aria-label','Condition group');option(select,'all','All conditions (AND)');option(select,'any','Any condition (OR)');select.value=key;select.addEventListener('change',()=>{const children=node[key];delete node[key];key=select.value;node[key]=children;save();});box.append(select);
      node[key].forEach((child,i)=>drawRule(box,child,()=>{node[key].splice(i,1);save();renderEditor();},save,target));
      function leaf(){return {field:choices[0]?.name||'',op:'eq',value:''};}
      box.append(button('+ Condition',()=>{node[key].push(leaf());save();renderEditor();}),button('+ Group',()=>{node[key].push({any:[leaf()]});save();renderEditor();}));
    } else {
      const field=el('select');field.setAttribute('aria-label','Condition field');choices.forEach(f=>option(field,f.name,f.label));field.value=node.field;field.addEventListener('change',()=>{node.field=field.value;node.op='eq';node.value='';save();renderEditor();});box.append(field);
      const dtype=fields.find(f=>f.name===node.field)?.type;const ops=[['eq','equals'],['ne','does not equal'],['empty','is empty'],['not_empty','is not empty']];
      if(['integer','bigint','float','decimal','currency','percent'].includes(dtype))ops.push(['gt','greater than'],['gte','at least'],['lt','less than'],['lte','at most']);
      if(['string','text','markdown','email','url','phone'].includes(dtype))ops.push(['contains','contains']);
      const op=el('select');op.setAttribute('aria-label','Condition operator');ops.forEach(([v,l])=>option(op,v,l));op.value=node.op;op.addEventListener('change',()=>{node.op=op.value;save();renderEditor();});box.append(op);
      if(!['empty','not_empty'].includes(node.op)) {
        const enumChoices=fields.find(f=>f.name===node.field)?.choices||[];const value=el(enumChoices.length||dtype==='boolean'?'select':'input');
        if(enumChoices.length){option(value,'','Choose value');enumChoices.forEach(v=>option(value,v,v));}else if(dtype==='boolean'){option(value,'true','Yes');option(value,'false','No');}
        value.setAttribute('aria-label','Condition value');value.value=node.value??'';value.addEventListener('input',()=>{node.value=value.value;save();});box.append(value);
      }
    }
    box.append(button('Remove condition',remove));
  }
  function render() {
    $('studio-title').value=config.title||'';$('studio-description').value=config.description||'';$('studio-purpose').value=config.purpose;$('studio-catalog').checked=config.in_catalog;$('studio-group').value=config.catalog_group||'';
    renderPalette();renderCanvas();renderEditor();
    $('studio-issues').replaceChildren(...(state.issues.length?state.issues.map(i=>el('p',i.message,'flash '+(i.level==='error'?'danger':'warning'))):[el('p','No configuration issues found.')]));
    $('studio-diff').replaceChildren();state.changes.forEach(change=>{const details=el('details');const name=change.setting.startsWith('field:')?byId(Number(change.setting.split(':')[1]))?.label||change.setting:change.setting;details.append(el('summary',name+' changed'),el('pre',JSON.stringify({before:change.before,after:change.after},null,2)));$('studio-diff').append(details);});
    if(!state.changes.length)$('studio-diff').append(el('p','No changes from the live form.','muted'));
    $('publish-status').textContent=state.stale?'The live configuration changed. Start fresh or restore a version, then review again.':state.tested?'Saved draft tested successfully. Ready for review.':'A successful preview is required before publishing.';
    $('studio-revisions').replaceChildren();state.revisions.forEach(r=>option($('studio-revisions'),r.id,r.label));updateButtons();
  }
  function renderPreview(result) {
    $('preview-status').textContent=result.message+(result.valid?' Test passed.':' Check the fields below, then run the preview again.');
    const panel=$('studio-preview');panel.replaceChildren();
    result.fields.forEach(field=>{
      const card=el('div',undefined,'preview-control'), labelNode=el('label',field.label+(field.required?' *':''));card.append(labelNode,el('p',field.reason,'muted'));
      if(field.visible&&field.access!=='none') {
        const control=el(field.options.length||field.type==='relation_mn'?'select':'input');control.disabled=field.readonly;
        control.multiple=field.type==='relation_mn';
        if(field.options.length)field.options.forEach(o=>option(control,o.value,o.label));
        else control.type=field.type==='date'?'date':field.type==='boolean'?'checkbox':'text';
        if(control.type==='checkbox')control.checked=sample[field.name]===true;else control.value=sample[field.name]??'';
        if(control.multiple)Array.from(control.options).forEach(o=>o.selected=(sample[field.name]||[]).map(String).includes(o.value));
        control.addEventListener('input',()=>{sample[field.name]=control.multiple?Array.from(control.selectedOptions).map(o=>o.value):control.type==='checkbox'?control.checked:control.value;state.tested=false;updateButtons();});labelNode.append(control);
      }
      field.errors.forEach(error=>card.append(el('p',error,'flash danger')));panel.append(card);
    });
  }
  [['studio-title','title'],['studio-description','description'],['studio-purpose','purpose'],['studio-group','catalog_group'],['studio-catalog','in_catalog']].forEach(([id,key])=>$(id).addEventListener('input',()=>{config[key]=$(id).type==='checkbox'?$(id).checked:$(id).value;settings();}));
  $('studio-section').onclick=()=>add({kind:'section',label_override:'New section'});
  $('draft-save').onclick=()=>request('save',{config});
  state.roles.forEach(r=>option($('preview-role'),r.name,r.label));$('preview-role').value=state.roles.some(r=>r.name==='user')?'user':state.roles[0]?.name;
  function users(initial=false){const select=$('preview-user');select.replaceChildren();option(select,'','Role only');state.users.filter(u=>u.role===$('preview-role').value).forEach(u=>option(select,u.id,u.label));if(!initial)state.tested=false;updateButtons();}
  $('preview-role').onchange=()=>users();users(true);
  $('preview-run').onclick=()=>request('preview',{role:$('preview-role').value,user_id:Number($('preview-user').value)||null,sample});
  $('draft-publish').onclick=()=>{if(confirm('Publish this saved form configuration for all users?'))request('publish');};
  $('draft-reset').onclick=()=>{if(confirm('Replace the draft with the current live form? Unsaved draft work will be lost.'))request('reset');};
  $('draft-restore').onclick=()=>{if(confirm('Replace this draft with the selected version?'))request('restore',{revision_id:Number($('studio-revisions').value)});};
  window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
  render();
})();
