const $ = (id) => document.getElementById(id);
const toolId = new URLSearchParams(location.search).get('id');

function pair(label, value) {
  const term = document.createElement('dt');
  term.textContent = label;
  const detail = document.createElement('dd');
  detail.textContent = value ?? '—';
  $('tool-info').append(term, detail);
}

function table(headers, rows) {
  const element = document.createElement('table');
  const head = document.createElement('thead');
  const headRow = document.createElement('tr');
  for (const label of headers) {
    const cell = document.createElement('th');
    cell.textContent = label;
    headRow.append(cell);
  }
  head.append(headRow);
  const body = document.createElement('tbody');
  for (const values of rows) {
    const row = document.createElement('tr');
    for (const value of values) {
      const cell = document.createElement('td');
      cell.textContent = value == null ? '—' : String(value);
      row.append(cell);
    }
    body.append(row);
  }
  element.append(head, body);
  return element;
}

async function load() {
  if (!toolId) throw new Error('缺少工具 ID');
  const response = await fetch(`/api/image-lab/tools/${encodeURIComponent(toolId)}`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const tool = await response.json();
  $('tool-name').textContent = tool.name;
  document.title = `${tool.name} · PixelForge`;
  $('tool-summary').textContent = tool.description;
  $('tool-state').textContent = `${tool.catalog_state} · ${tool.availability_reason || ''}`;
  pair('ID', tool.id);
  pair('类别', tool.category);
  pair('函数', tool.function_name);
  pair('来源', tool.source);
  pair('版本', tool.current_version);
  pair('可用性', tool.availability_reason);
  $('tool-parameters').append(table(['参数', '类型', '默认值', '范围/选项', '显示条件'],
    (tool.params_schema || []).map((param) => [param.label, param.kind, param.default,
      param.options.length ? param.options.join(', ') : `${param.minimum ?? '—'} ~ ${param.maximum ?? '—'}`,
      Object.keys(param.when || {}).length ? JSON.stringify(param.when) : '始终'])));
  $('tool-ports').append(table(['方向', '端口', '类型'], [
    ['输入', 'image', 'IMAGE_RGB8'],
    ...Object.entries(tool.outputs || {}).map(([port, kind]) => ['输出', port, kind]),
  ]));
  $('try-tool').href = `tools.html?id=${encodeURIComponent(tool.id)}`;
  const examples = await fetch(`/api/image-lab/tools/${encodeURIComponent(tool.id)}/examples`).then((r) => r.json());
  $('example-list').textContent = `${examples.length} 组版本示例`;
  const sourceId = examples[0]?.input_refs?.image?.asset_id;
  if (sourceId) $('example-source').src = `/api/image-lab/assets/${encodeURIComponent(sourceId)}/content`;
  if (tool.effect_image) {
    $('effect-image').src = tool.effect_image;
    $('effect-image').hidden = false;
    $('example-state').textContent = '该图由当前工具作用于左侧合成图生成。实际效果仍需用目标图片验证。';
  } else {
    $('example-state').textContent = tool.availability_reason || '该工具尚无可运行示例。';
  }
}

load().catch((error) => { $('tool-summary').textContent = `读取失败：${error.message}`; });
