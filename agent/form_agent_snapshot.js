// Lists a page's visible form controls for the form agent, tagging each with an id.
// Called with [attribute, limit]; see agent/form_agent.py.
([attribute, limit]) => {
  const visible = (el) => {
    // File inputs are often hidden behind a styled "Browse" button but still accept files.
    if (el.type === 'file') return true;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    return el.getClientRects().length > 0;
  };
  const text = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const FIELDS = 'input:not([type=hidden]), textarea, select, [role=combobox], [role=checkbox], ' +
    '[role=radio], [role=textbox], [contenteditable=true]';
  const LABELS = 'label, legend, [class*=label], [class*=Label], [class*=question], h3, h4';
  // The question printed near a field that is not linked to it in code (common on custom
  // forms): the closest label-like text in the smallest surrounding box, stopping once the
  // box holds another field. skip lists texts that are not the question (option labels).
  const nearbyLabel = (el, skip = []) => {
    let box = el.parentElement;
    for (let i = 0; i < 6 && box && box !== document.body; i++, box = box.parentElement) {
      const found = Array.from(box.querySelectorAll(LABELS)).find((label) => {
        const words = text(label);
        return !label.contains(el) && !label.querySelector(FIELDS) &&
          words.replace(/[^A-Za-z0-9]/g, '').length >= 3 && !skip.includes(words);
      });
      if (found) return text(found).slice(0, 300);
      const others = Array.from(box.querySelectorAll(FIELDS))
        .filter((other) => other !== el && !el.contains(other) && !other.contains(el));
      if (others.length && !skip.length) return '';
    }
    return '';
  };
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
    if (!parts.join('').trim() && el.type !== 'file') {
      const near = nearbyLabel(el);
      if (near) parts.push(near);
    }
    if (!parts.join('').trim() && el.type === 'file') {
      // An unlabelled upload: name it by the nearest text around it ("Resume", "Browse").
      let around = el.parentElement;
      while (around && !text(around) && around !== document.body) around = around.parentElement;
      let field = around;
      for (let i = 0; i < 4 && field && field.parentElement; i++) {
        const label = field.parentElement.querySelector('label, legend, [class*=label]');
        if (label && text(label)) { parts.push(text(label)); break; }
        field = field.parentElement;
      }
      if (!parts.length && around) parts.push(text(around).slice(0, 120));
    }
    if (el.placeholder) parts.push('placeholder: ' + el.placeholder);
    return parts.filter(Boolean).join(' | ').slice(0, 300);
  };
  const groupOf = (el) => {
    const group = el.closest('fieldset, [role=radiogroup], [role=group]');
    if (group) {
      const legend = group.querySelector('legend') || document.getElementById(group.getAttribute('aria-labelledby') || '');
      const named = text(legend) || group.getAttribute('aria-label') || '';
      if (named) return named;
    }
    if ((el.type === 'checkbox' || el.type === 'radio') && el.name) {
      // Options sharing a name answer one question, printed outside the box that holds
      // all the options (their own texts are inside it).
      const options = Array.from(document.getElementsByName(el.name));
      let group = el.parentElement;
      while (group && group !== document.body && !options.every((option) => group.contains(option))) {
        group = group.parentElement;
      }
      let box = group && group !== document.body ? group.parentElement : null;
      for (let i = 0; i < 4 && box && box !== document.body; i++, box = box.parentElement) {
        const found = Array.from(box.querySelectorAll(LABELS)).find((label) =>
          !group.contains(label) && !label.contains(group) && !label.querySelector(FIELDS) &&
          text(label).replace(/[^A-Za-z0-9]/g, '').length >= 3);
        if (found) return text(found).slice(0, 300);
      }
    }
    return '';
  };
  const selector = 'input:not([type=hidden]), textarea, select, button, a[href], [role=button], ' +
    '[role=combobox], [role=listbox], [role=option], [role=radio], [role=checkbox], ' +
    '[role=switch], [role=textbox], [role=menuitem], [role=tab], [contenteditable=true]';
  // Ids restart at 1 on every read: clear the old ones, which stay on controls of earlier
  // pages that are hidden but still in the document, so an id names one control only.
  for (const old of document.querySelectorAll('[' + attribute + ']')) old.removeAttribute(attribute);
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
