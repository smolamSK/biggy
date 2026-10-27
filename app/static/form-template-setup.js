(function () {
  'use strict';
  const data = JSON.parse(document.getElementById('template-setup-data').textContent);
  const $ = id => document.getElementById(id);
  const fieldSelects = {}, relationSelects = {}, filterSelects = {};
  let review = null, busy = false;
  function el(tag, text) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; }
  function select(parent, title, options, suggested) {
    const label = el('label', title), control = el('select');
    [ {id: '', label: 'Choose a match'}, ...options ].forEach(choice => {
      const option = el('option', choice.label); option.value = choice.id; control.append(option);
    });
    control.value = suggested || ''; label.append(control); parent.append(label); return control;
  }
  function step(number, focus = true) {
    document.querySelectorAll('.wizard-page').forEach(page => page.hidden = Number(page.dataset.page) !== number);
    document.querySelectorAll('[data-wizard-step]').forEach(item => {
      if (Number(item.dataset.wizardStep) === number) item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
    });
    if (focus) document.querySelector(`[data-page="${number}"] h2`).focus();
  }
  function dependentFilters() {
    $('wizard-filters').replaceChildren(); Object.keys(filterSelects).forEach(key => delete filterSelects[key]);
    data.template.items.filter(item => item.parent).forEach(item => {
      const child = fieldSelects[item.field]?.value, parent = fieldSelects[item.parent]?.value;
      const options = (data.related[child] || []).filter(field => field.target === data.targets[parent]);
      const matching = options.find(field => field.name === item.filter);
      filterSelects[item.field] = select($('wizard-filters'), `${data.template.fields[item.field].label}: match the controlling field through`,
        options, matching?.id || (options.length === 1 ? options[0].id : null));
      filterSelects[item.field].addEventListener('change', () => { review = null; });
    });
  }
  Object.entries(data.template.fields).forEach(([key, spec]) => {
    const options = data.fields[key];
    const sameName = options.find(field => field.name === key);
    const control = select($('wizard-fields'), `${spec.label} (${spec.type})`, options, sameName?.id || (options.length === 1 ? options[0].id : null));
    fieldSelects[key] = control;
    if (spec.choices.length) $('wizard-fields').append(el('p', 'Required choices: ' + spec.choices.join(', ')));
    if (!options.length) { const message = el('p', `No compatible field for ${spec.label}. Add one on the target table before continuing.`); message.className = 'flash warning'; $('wizard-fields').append(message); }
    control.addEventListener('change', () => { review = null; dependentFilters(); });
  });
  Object.entries(data.template.relations).forEach(([key, spec]) => {
    const sameName = data.relations.find(relation => relation.label === spec.label);
    relationSelects[key] = select($('wizard-relations'), spec.label + ' (many-to-many)', data.relations, sameName?.id);
    relationSelects[key].addEventListener('change', () => { review = null; });
  });
  dependentFilters();
  function payload() {
    const mappings = controls => Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, Number(control.value) || null]));
    return {fields: mappings(fieldSelects), relations: mappings(relationSelects), filters: mappings(filterSelects)};
  }
  async function post(url, body) {
    if (busy) return null;
    busy = true; document.querySelectorAll('.wizard-page').forEach(page => page.inert = true); $('wizard-status').textContent = 'Checking configuration…';
    try {
      const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('[name=template-csrf]').content}, body: JSON.stringify(body)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'The request failed. Reload and try again.');
      $('wizard-status').textContent = ''; return result;
    } catch (error) { $('wizard-status').textContent = error.message; return null; }
    finally { busy = false; document.querySelectorAll('.wizard-page').forEach(page => page.inert = false); }
  }
  const operators = {eq: 'equals', ne: 'does not equal', empty: 'is empty', not_empty: 'is not empty', contains: 'contains', gt: 'is greater than', gte: 'is at least', lt: 'is less than', lte: 'is at most'};
  function condition(node) {
    if (node.all || node.any) return '(' + (node.all || node.any).map(condition).join(node.all ? ' AND ' : ' OR ') + ')';
    return (review.field_labels[node.field] || node.field) + ' ' + operators[node.op] + (['empty', 'not_empty'].includes(node.op) ? '' : ' “' + node.value + '”');
  }
  function renderReview() {
    const controls = $('wizard-behavior'); controls.replaceChildren();
    review.config.items.forEach(item => {
      const source = Object.values(data.fields).flat().find(field => field.id === item.field_id);
      const relation = data.relations.find(relation => relation.id === item.relation_id);
      const title = item.label_override || source?.label || relation?.label || 'Section';
      const block = el('div'); block.className = 'wizard-control'; block.append(el('h3', title));
      if (item.required) block.append(el('p', 'Always required'));
      if (item.readonly) block.append(el('p', 'Always read-only'));
      if (item.parent_field_id) block.append(el('p', 'Choices are filtered by the selected parent record.'));
      Object.entries(JSON.parse(item.rules_json || '{}')).forEach(([action, rule]) => {
        const prefix = {show: 'Show when ', require: 'Require when ', readonly: 'Read-only when '}[action];
        block.append(el('p', prefix + condition(rule)));
      });
      controls.append(block);
    });
    $('wizard-issues').replaceChildren(...review.issues.map(issue => { const node = el('p', issue.message); node.className = 'flash ' + (issue.level === 'error' ? 'danger' : 'warning'); return node; }));
    $('wizard-permissions').replaceChildren(...review.permissions.map(permission => {
      const row = el('tr'); [permission.role, permission.access, permission.restricted_fields].forEach(value => row.append(el('td', String(value)))); return row;
    }));
    const changes = $('wizard-changes'); changes.replaceChildren(el('p', `${review.config.items.length} controls in the proposed draft. ${review.changes.length} configuration changes compared with the live form.`));
    changes.append(el('p', 'The target form keeps its name, title, catalog visibility and permissions. Its layout, description, purpose and conditions come from the template.'));
    $('wizard-replace-label').hidden = !review.replace_draft; $('wizard-replace').checked = false;
  }
  $('wizard-review').onclick = async () => { const result = await post(data.review_url, payload()); if (result) { review = result; renderReview(); step(3); } };
  document.querySelectorAll('[data-go]').forEach(button => button.addEventListener('click', () => {
    const next = Number(button.dataset.go);
    if (next > 2 && !review) { $('wizard-status').textContent = 'Review the current mappings first.'; step(2); return; }
    step(next);
  }));
  $('wizard-apply').onclick = async () => {
    if (!review) { step(2); return; }
    if (review.replace_draft && !$('wizard-replace').checked) { $('wizard-status').textContent = 'Choose whether to replace the existing saved draft.'; return; }
    const result = await post(data.apply_url, {...payload(), token: review.token, replace_draft: $('wizard-replace').checked});
    if (result) location.assign(result.url + '#role-preview');
  };
  step(2, false);
})();
