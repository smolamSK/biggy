(function () {
  const from = document.querySelector('[name=from_table_id]');
  const to = document.querySelector('[name=to_table_id]');
  const name = document.querySelector('[name=name]');
  const column = document.querySelector('[name=field_name]');
  const summary = document.getElementById('relationship-summary');
  if (!from || !to || !summary) return;
  let generatedName = '', generatedColumn = '';
  function update() {
    const a = from.selectedOptions[0]?.textContent || 'Record';
    const b = to.selectedOptions[0]?.textContent || 'Record';
    summary.textContent = column ? `Each ${a} links to one ${b}. A ${b} can have many ${a} records.` : `${a} and ${b} can each link to multiple records.`;
    const nextName = `${a} → ${b}`;
    const nextColumn = b.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '') + '_id';
    if (name && (!name.value || name.value === generatedName)) name.value = nextName;
    if (column && (!column.value || column.value === generatedColumn)) column.value = /^[a-z]/.test(nextColumn) ? nextColumn : 'related_' + nextColumn;
    generatedName = nextName; generatedColumn = /^[a-z]/.test(nextColumn) ? nextColumn : 'related_' + nextColumn;
  }
  from.addEventListener('change', update); to.addEventListener('change', update); update();
})();
