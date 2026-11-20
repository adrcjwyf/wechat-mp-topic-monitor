(() => {
  'use strict';

  const form = document.getElementById('rule-form');
  if (!form) return;

  const labels = {include: '入选词', maybe: '疑似词', exclude: '排除词'};
  const keys = Object.keys(labels);
  const delimiter = /[,，、;；\s]+/u;
  const cards = [...form.querySelectorAll('.rule-account')];
  const nameInput = form.querySelector('#rule-name');
  const status = form.querySelector('.rule-status');
  const payload = form.elements.namedItem('keywords');
  const editors = [];
  let rules;

  function report(message, error = false) {
    status.textContent = message;
    status.dataset.error = String(error);
  }

  function changed() {
    report('修改尚未保存。');
  }

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content) node.textContent = content;
    return node;
  }

  function drawPreview(card) {
    const rows = keys.map(key => {
      const row = element('div', 'rule-' + key);
      const chips = element('div', 'rule-chips');
      chips.append(...rules[key].map(term => element('span', 'rule-chip', term)));
      row.append(element('strong', '', labels[key] + '（' + rules[key].length + '）'), chips);
      return row;
    });
    card.querySelector('.rule-preview').replaceChildren(...rows);
  }

  function updateInheritance() {
    const count = keys.reduce((sum, key) => sum + rules[key].length, 0);
    for (const card of cards) {
      card.querySelector('.rule-count').textContent = count;
      if (card.open && card.querySelector('.rule-inherited').open) drawPreview(card);
    }
  }

  function addGroup(container, owner, key) {
    const isGlobal = owner === rules;
    const labelText = (isGlobal ? '' : '专属') + labels[key];
    const group = element('div', 'rule-group rule-' + key);
    const input = element('input');
    input.type = 'text';
    input.id = 'rule-term-' + editors.length;
    input.autocomplete = 'off';
    input.placeholder = '回车添加，可批量粘贴';
    const label = element('label', '', labelText);
    label.htmlFor = input.id;
    const chips = element('div', 'rule-chips');
    const addRow = element('div', 'rule-add');
    const add = element('button', 'secondary', '添加');
    add.type = 'button';
    addRow.append(input, add);
    group.append(label, chips, addRow);
    container.append(group);

    function repaint() {
      const items = owner[key].map((term, index) => {
        const chip = element('span', 'rule-chip');
        const remove = element('button', '', '×');
        remove.type = 'button';
        remove.setAttribute('aria-label', '删除' + labelText + '「' + term + '」');
        remove.addEventListener('click', () => {
          owner[key].splice(index, 1);
          repaint();
          if (isGlobal) updateInheritance();
          changed();
          (chips.children[index]?.querySelector('button') || input).focus();
        });
        chip.append(element('span', '', term), remove);
        return chip;
      });
      chips.replaceChildren(...items);
    }

    function commit() {
      if (!input.value.trim()) return;
      const terms = input.value.split(delimiter).filter(Boolean);
      const unique = new Set([...owner[key], ...terms]);
      let problem;
      if (terms.some(term => [...term].length > 256)) problem = '每个关键词最多 256 个字符。';
      if (unique.size > 512) problem = '每组最多 512 个关键词。';
      if (problem) {
        const card = input.closest('.rule-account');
        if (card) card.open = true;
        input.focus();
        throw new Error(problem);
      }
      input.value = '';
      if (unique.size === owner[key].length) return;
      owner[key] = [...unique];
      repaint();
      if (isGlobal) updateInheritance();
      changed();
    }

    function addTerms() {
      try {
        commit();
        input.focus();
      } catch (error) {
        report(error.message, true);
      }
    }

    input.addEventListener('keydown', event => {
      if (!event.isComposing && event.keyCode !== 229 && event.key === 'Enter') {
        event.preventDefault();
        addTerms();
      }
    });
    input.addEventListener('paste', event => {
      const pasted = event.clipboardData?.getData('text');
      if (!pasted || !delimiter.test(pasted)) return;
      event.preventDefault();
      input.setRangeText(pasted.split(delimiter).join('，'), input.selectionStart, input.selectionEnd, 'end');
      addTerms();
    });
    add.addEventListener('click', addTerms);
    editors.push(commit);
    repaint();
  }

  function openAnchor() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
    const card = document.getElementById(id);
    if (!card || !cards.includes(card)) return;
    card.open = true;
    card.scrollIntoView({block: 'start'});
  }

  try {
    rules = JSON.parse(form.querySelector('#rule-initial').value);
    for (const key of keys) addGroup(form.querySelector('#rule-global-groups'), rules, key);
    for (const card of cards) {
      const id = card.dataset.accountId;
      if (!Object.hasOwn(rules.accounts, id)) {
        Object.defineProperty(rules.accounts, id, {value: {include: [], exclude: []}, enumerable: true});
      }
      for (const key of ['include', 'exclude']) {
        addGroup(card.querySelector('.rule-account-groups'), rules.accounts[id], key);
      }
      const inherited = card.querySelector('.rule-inherited');
      function refreshIfOpen() {
        if (card.open && inherited.open) drawPreview(card);
      }
      card.addEventListener('toggle', refreshIfOpen);
      inherited.addEventListener('toggle', refreshIfOpen);
    }
    updateInheritance();
  } catch (error) {
    report('关键词加载失败，请刷新页面。', true);
    return;
  }

  form.addEventListener('input', changed);
  form.addEventListener('submit', event => {
    try {
      rules.name = nameInput.value.trim();
      if (!rules.name) {
        nameInput.focus();
        throw new Error('请填写专题名称。');
      }
      for (const commit of editors) commit();
      payload.value = JSON.stringify(rules);
      payload.disabled = false;
    } catch (error) {
      event.preventDefault();
      payload.disabled = true;
      report(error.message, true);
    }
  });
  form.querySelector('.rule-controls').disabled = false;
  window.addEventListener('hashchange', openAnchor);
  openAnchor();
})();
