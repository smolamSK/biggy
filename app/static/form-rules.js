/* Form behavior is repeated on the server; these rules are not permissions. */
(function () {
  'use strict';
  const numeric = new Set(['integer','bigint','float','decimal','currency','percent']);
  const text = v => v === null || v === undefined ? '' : String(v);
  function decimal(value) {
    const m=value.trim().match(/^([+-]?)(\d*)(?:\.(\d*))?(?:e([+-]?\d+))?$/i);
    if(!m || !(m[2] || m[3])) return null;
    const digits=(m[2]+(m[3]||'')).replace(/^0+/, '');
    return {sign:digits ? (m[1]==='-'?-1:1) : 0, digits,
      magnitude:BigInt(digits.length)-BigInt((m[3]||'').length)+BigInt(m[4]||0)};
  }
  function compare(a,b) {
    if(a.sign!==b.sign)return a.sign>b.sign?1:-1;
    if(!a.sign)return 0;
    if(a.magnitude!==b.magnitude)return (a.magnitude>b.magnitude?1:-1)*a.sign;
    const n=Math.max(a.digits.length,b.digits.length), x=a.digits.padEnd(n,'0'), y=b.digits.padEnd(n,'0');
    return (x===y?0:x>y?1:-1)*a.sign;
  }
  function match(node, values, config) {
    if (node.all) return node.all.every(c => match(c, values, config));
    if (node.any) return node.any.some(c => match(c, values, config));
    if (!(node.field in values)) return false;
    let a = text(values[node.field]), b = text(node.value);
    if (node.op === 'empty') return a === '';
    if (node.op === 'not_empty') return a !== '';
    if (node.op === 'contains') return a.includes(b);
    if (numeric.has(config[node.field]?.type)) {
      if (a === '' || b === '') return false;
      a = decimal(a); b = decimal(b); if (!a || !b) return false;
      a = compare(a,b); b = 0;
    }
    return {eq:()=>a===b, ne:()=>a!==b, gt:()=>a>b, gte:()=>a>=b, lt:()=>a<b, lte:()=>a<=b}[node.op]();
  }
  function refs(node) { return node.field ? [node.field] : (node.all || node.any || []).flatMap(refs); }
  function states(config, values, trusted) {
    values = {...values}; const result = {}, visiting = new Set();
    Object.entries(config).forEach(([name,item]) => { if(item.locked) values[name] = trusted[name]; });
    function visit(name) {
      if(result[name]) return;
      if(visiting.has(name)) throw new Error('Conditional rule cycle');
      visiting.add(name);
      const item=config[name], rules=item.rules || {};
      Object.values(rules).flatMap(refs).forEach(dep=>{if(config[dep]) visit(dep);});
      const visible=rules.show ? match(rules.show,values,config) : true;
      const readonly=!!item.locked || (rules.readonly ? match(rules.readonly,values,config) : false);
      const required=(!!item.required || (rules.require ? match(rules.require,values,config) : false)) && visible && !readonly;
      result[name]={visible,readonly,required};
      if(!visible || readonly) values[name]=trusted[name];
      visiting.delete(name);
    }
    Object.keys(config).forEach(visit); return result;
  }
  window.BiggyFormRules={states,match};
  const el=document.getElementById('form-rule-config'); if(!el) return;
  const config=JSON.parse(el.textContent), trusted=JSON.parse(document.getElementById('form-rule-trusted').textContent);
  function apply() {
    const values={};
    Object.keys(config).forEach(name=>{const input=document.getElementsByName(name)[0]; if(input) values[name]=input.type==='checkbox'?input.checked:input.value;});
    const result=states(config,values,trusted);
    document.querySelectorAll('[data-rule-field]').forEach(wrap=>{
      const state=result[wrap.dataset.ruleField]; if(!state) return;
      wrap.hidden=!state.visible;
      wrap.querySelectorAll('input:not([type=hidden]),select,textarea').forEach(input=>{
        input.disabled=!state.visible || state.readonly;
        input.required=state.required;
      });
    });
  }
  document.addEventListener('input',apply); document.addEventListener('change',apply); apply();
})();
