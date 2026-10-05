// Lists a page's visible form controls for the form agent, tagging each with an id.
// Called with [attribute, limit]; see agent/form_agent.py.
([attribute, limit]) => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    return el.getClientRects().length > 0 || el.type === 'file';
  };
  const text = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const labelOf = (el) => {
    const parts = [];
    const aria = el.getAttribute('aria-label');
    if (aria) parts.push(aria);
    const by = el.getAttribute('aria-labelledby');
    if (by) by.split(/\s+/).forEach((id) => parts.push(text(document.getElementById(id))));
    if (el.labels) Array.from(el.labels).forEach((label) => parts.push(text(label)));
    if (!parts.join('').trim()) {
      const field = el.closest('fieldset, [role=group], [role=radiogroup], [data-automation-id]');
      const legend = field && field.querySelector('legend, label, [id$=label], h3, h4');
      if (legend && legend !== el) parts.push(text(legend));
    }
    if (el.placeholder) parts.push('placeholder: ' + el.placeholder);
    return parts.filter(Boolean).join(' | ').slice(0, 300);
  };
  const groupOf = (el) => {
    const group = el.closest('fieldset, [role=radiogroup], [role=group]');
    if (!group) return '';
    const legend = group.querySelector('legend') || document.getElementById(group.getAttribute('aria-labelledby') || '');
    return text(legend) || group.getAttribute('aria-label') || '';
  };
  const selector = 'input:not([type=hidden]), textarea, select, button, a[href], [role=button], ' +
    '[role=combobox], [role=listbox], [role=option], [role=radio], [role=checkbox], ' +
    '[role=switch], [role=textbox], [role=menuitem], [role=tab], [contenteditable=true]';
  const controls = [];
  let next = 0;
  for (const el of document.querySelectorAll(selector)) {
    if (controls.length >= limit) break;
    if (!visible(el)) continue;
    if (el.tagName === 'A' && !el.getAttribute('role') && !/apply|next|continue|back/i.test(text(el))) continue;
    const id = String(++next);
    el.setAttribute(attribute, id);
    const item = {
      id,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase() || undefined,
      role: el.getAttribute('role') || undefined,
      label: labelOf(el) || undefined,
      text: ['BUTTON', 'A', 'OPTION'].includes(el.tagName) || el.getAttribute('role') ? text(el).slice(0, 120) || undefined : undefined,
      group: groupOf(el) || undefined,
      required: el.required || el.getAttribute('aria-required') === 'true' || undefined,
      disabled: el.disabled || el.getAttribute('aria-disabled') === 'true' || undefined,
    };
    if (el.tagName === 'SELECT') {
      item.options = Array.from(el.options).slice(0, 60).map((option) => option.text.trim());
      item.value = el.selectedOptions.length ? el.selectedOptions[0].text.trim() : '';
    } else if (el.type === 'checkbox' || el.type === 'radio') {
      item.checked = el.checked;
    } else if ('value' in el && el.type !== 'file') {
      item.value = String(el.value || '').slice(0, 200) || undefined;
    } else if (el.getAttribute('aria-checked')) {
      item.checked = el.getAttribute('aria-checked') === 'true';
    }
    if (el.type === 'file') item.file = el.files && el.files.length ? el.files[0].name : '';
    controls.push(item);
  }
  const headings = Array.from(document.querySelectorAll('h1, h2, h3')).filter(visible).map(text).filter(Boolean).slice(0, 12);
  const alerts = Array.from(document.querySelectorAll('[role=alert], [aria-live=assertive], .error, [class*=error]'))
    .filter(visible).map(text).filter(Boolean).slice(0, 10);
  return { url: location.href, title: document.title, headings, alerts, controls };
}
