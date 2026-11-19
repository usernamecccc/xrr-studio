/* XRR 拟合工作台 —— M2：层结构 + 参数表 + 仿真 + 差分进化拟合 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var state = {
    datasets: [],
    activeId: null,
    stack: {
      ambient: { formula: 'Air', density: 0.0012 },
      segments: [
        {
          repetitions: 1,
          layers: [
            { formula: 'Mo', density: 10.28, thickness: 28, roughness: 3 },
            { formula: 'Si', density: 2.33, thickness: 42, roughness: 4 }
          ]
        }
      ],
      substrate: { formula: 'Si', density: 2.329 }
    },
    instrument: {
      wavelength: 1.54, i0: 1, ibkg: 0,
      resolution: { type: 'none', width: 0.001, points: 5, spread: 2 },
      footprint: { type: 'none', beam_width: 0.05, sample_length: 10 }
    },
    params: [],
    bulk: {},
    elements: [],
    sim: null,
    fitJob: null,
    fitTimer: null,
    fitRunning: false,
    fitHistory: [],
    fitGen: 0,
    fitMaxGen: 0,
    fitBest: null,
    plotMode: 'data',
    busy: false,
    seq: 0
  };

  // ---------------------------------------------------------------- 主题
  function applyTheme(ctx) {
    var dark = ctx && ctx.theme === 'dark';
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    draw();
  }
  if (window.wgallop && wgallop.getContext) {
    wgallop.getContext().then(applyTheme).catch(function () { draw(); });
    if (wgallop.onContextChanged) wgallop.onContextChanged(applyTheme);
  }

  function setStatus(text, isError) {
    var el = $('status');
    el.textContent = text;
    el.classList.toggle('is-error', !!isError);
  }

  function fmt(value) {
    if (value === null || value === undefined || !isFinite(value)) return '—';
    var abs = Math.abs(value);
    if (abs === 0) return '0';
    if (abs >= 1e-3 && abs < 1e5) return value.toPrecision(5);
    return value.toExponential(3);
  }

  function numberOr(value, fallback) {
    var n = Number(value);
    return isFinite(n) ? n : fallback;
  }

  // ---------------------------------------------------------------- 数据
  function activeDataset() {
    for (var i = 0; i < state.datasets.length; i++) {
      if (state.datasets[i].id === state.activeId) return state.datasets[i];
    }
    return null;
  }

  function parseText(text) {
    var lines = text.split(/\r?\n/);

    // 先扫注释头，识别列含义（GenX 导出的 .dat 第 2 列是模拟值）
    var header = '';
    for (var h = 0; h < Math.min(lines.length, 60); h += 1) {
      if (/^\s*[#;!\/]/.test(lines[h])) header += ' ' + lines[h];
    }
    var xIndex = 0;
    var yIndex = 1;
    var errIndex = 2;
    var note = '';
    if (/I_simulated/i.test(header)) {
      // GenX 导出：x, I_simulated, I, error(I)
      yIndex = 2;
      errIndex = 3;
      note = '检测到 GenX 导出格式，改用第 3 列（实测强度）作为数据';
    }

    var rows = [];
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim();
      if (!line) continue;
      if (/^[#;!\/]/.test(line)) continue;
      var parts = line.split(/[\s,;\t]+/).filter(function (p) { return p !== ''; });
      if (parts.length <= Math.max(xIndex, yIndex)) continue;
      var nums = parts.map(Number);
      var xv = nums[xIndex];
      var yv = nums[yIndex];
      if (!isFinite(xv) || !isFinite(yv)) continue;
      var ev = (errIndex < nums.length && isFinite(nums[errIndex])) ? nums[errIndex] : null;
      rows.push([xv, yv, ev]);
    }
    if (note) rows.note = note;
    return rows;
  }

  var UNITS = ['q', '2theta', 'theta'];

  function unitLabel(unit) {
    if (unit === 'q') return 'q';
    if (unit === 'theta') return 'θ';
    return '2θ';
  }

  function unitAxisLabel(unit) {
    if (unit === 'q') return 'q (Å⁻¹)';
    if (unit === 'theta') return 'θ (°)';
    return '2θ (°)';
  }

  function nextUnit(unit) {
    var index = UNITS.indexOf(unit);
    return UNITS[(index + 1) % UNITS.length];
  }

  function guessUnit(xs) {
    var max = 0;
    for (var i = 0; i < xs.length; i++) max = Math.max(max, Math.abs(xs[i]));
    return max > 5 ? '2theta' : 'q';
  }

  function addDataset(name, rows) {
    if (!rows.length) return null;
    var xs = rows.map(function (r) { return r[0]; });
    var ys = rows.map(function (r) { return r[1]; });
    var errs = rows.map(function (r) { return r.length > 2 ? r[2] : null; });
    var hasError = errs.some(function (v) { return v !== null && isFinite(v) && v > 0; });
    if (!hasError) errs = null;
    state.seq += 1;
    var ds = { id: 'ds' + state.seq, name: name, x: xs, y: ys, error: errs, xUnit: guessUnit(xs) };
    state.datasets.push(ds);
    state.activeId = ds.id;
    renderDataList();
    return ds;
  }

  function renderDataList() {
    var list = $('data-list');
    var count = $('data-count');
    list.innerHTML = '';
    count.textContent = String(state.datasets.length);
    if (!state.datasets.length) {
      var empty = document.createElement('li');
      empty.className = 'sidebar-empty';
      empty.textContent = '还没有数据。点下方「导入数据…」选择 .dat / .txt / .csv 文本文件（两列或三列：x、强度、误差）。';
      list.appendChild(empty);
      return;
    }
    state.datasets.forEach(function (ds) {
      var li = document.createElement('li');
      li.className = 'data-item' + (ds.id === state.activeId ? ' is-active' : '');
      var dot = document.createElement('span');
      dot.className = 'dot';
      var name = document.createElement('span');
      name.className = 'name';
      name.textContent = ds.name;
      var meta = document.createElement('span');
      meta.className = 'meta';
      meta.textContent = ds.x.length + ' 点' + (ds.error ? '' : ' · 无误差列');
      var unitBtn = document.createElement('button');
      unitBtn.type = 'button';
      unitBtn.className = 'unit-btn';
      unitBtn.textContent = unitLabel(ds.xUnit);
      unitBtn.title = '切换横坐标单位：q → 2θ → θ';
      unitBtn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        ds.xUnit = nextUnit(ds.xUnit);
        renderDataList();
        setStatus('横坐标单位改为 ' + unitAxisLabel(ds.xUnit) + '，按 F9 重新仿真');
      });
      li.appendChild(dot);
      li.appendChild(name);
      li.appendChild(meta);
      li.appendChild(unitBtn);
      li.addEventListener('click', function () {
        state.activeId = ds.id;
        renderDataList();
        draw();
      });
      list.appendChild(li);
    });
  }

  function importFiles(files) {
    var list = Array.prototype.slice.call(files || []);
    if (!list.length) return;
    var pending = list.length;
    list.forEach(function (file) {
      var reader = new FileReader();
      reader.onload = function () {
        try {
          var rows = parseText(String(reader.result || ''));
          if (!rows.length) {
            setStatus('无法从 ' + file.name + ' 解析出数据列', true);
          } else {
            addDataset(file.name.replace(/\.[^.]+$/, ''), rows);
            setStatus('已导入 ' + file.name + '（' + rows.length + ' 点）'
              + ' · 编辑层结构后按 F9 仿真');
          }
        } catch (err) {
          setStatus('读取失败：' + err.message, true);
        } finally {
          pending -= 1;
          if (pending === 0) renderDataList();
        }
      };
      reader.onerror = function () {
        setStatus('读取 ' + file.name + ' 失败', true);
        pending -= 1;
      };
      reader.readAsText(file, 'utf-8');
    });
  }

  // ---------------------------------------------------------------- 层结构
  function makeInput(value, step, onChange) {
    var input = document.createElement('input');
    input.type = 'number';
    input.step = step;
    input.value = String(value);
    input.addEventListener('change', function () {
      var n = Number(input.value);
      if (!isFinite(n)) { input.classList.add('is-invalid'); return; }
      input.classList.remove('is-invalid');
      onChange(n);
    });
    return input;
  }

  function makeText(value, onChange) {
    var input = document.createElement('input');
    input.type = 'text';
    input.value = String(value);
    input.addEventListener('change', function () {
      var v = input.value.trim();
      if (!v) { input.classList.add('is-invalid'); return; }
      input.classList.remove('is-invalid');
      onChange(v);
    });
    return input;
  }

  function makeTag(text) {
    var span = document.createElement('span');
    span.className = 'tag';
    span.textContent = text;
    return span;
  }

  function blank() {
    return document.createElement('span');
  }

  function updateLayerCount() {
    var total = 0;
    state.stack.segments.forEach(function (seg) {
      total += (seg.layers.length || 0) * (seg.repetitions || 1);
    });
    var el = $('layer-count');
    if (el) el.textContent = '共 ' + total + ' 层';
  }

  function renderStack() {
    var host = $('stack-rows');
    host.innerHTML = '';

    var amb = document.createElement('div');
    amb.className = 'stack-row is-ambient';
    amb.appendChild(makeTag('环境'));
    amb.appendChild(makeText(state.stack.ambient.formula, function (v) { state.stack.ambient.formula = v; }));
    amb.appendChild(makeInput(state.stack.ambient.density, '0.001', function (v) {
      state.stack.ambient.density = v;
      syncParamValue('ambient.density', v);
    }));
    amb.appendChild(blank());
    amb.appendChild(blank());
    amb.appendChild(blank());
    host.appendChild(amb);

    state.stack.segments.forEach(function (seg, segIndex) {
      var head = document.createElement('div');
      head.className = 'seg-head';
      head.appendChild(makeTag('段 ' + (segIndex + 1)));

      var repWrap = document.createElement('span');
      repWrap.className = 'seg-rep-wrap';
      var repLabel = document.createElement('span');
      repLabel.className = 'seg-rep-label';
      repLabel.textContent = '重复';
      var repInput = document.createElement('input');
      repInput.type = 'number';
      repInput.min = '1';
      repInput.step = '1';
      repInput.value = String(seg.repetitions || 1);
      repInput.className = 'seg-rep';
      repInput.addEventListener('change', function () {
        var n = Math.max(1, Math.round(numberOr(repInput.value, 1)));
        seg.repetitions = n;
        repInput.value = String(n);
        updateLayerCount();
        draw();
      });
      var repUnit = document.createElement('span');
      repUnit.className = 'seg-rep-unit';
      repUnit.textContent = '次';
      repWrap.appendChild(repLabel);
      repWrap.appendChild(repInput);
      repWrap.appendChild(repUnit);
      head.appendChild(repWrap);

      var delSeg = document.createElement('button');
      delSeg.type = 'button';
      delSeg.className = 'btn-icon';
      delSeg.title = '删除该段';
      delSeg.textContent = '✕';
      delSeg.addEventListener('click', function () {
        if (state.stack.segments.length <= 1) {
          setStatus('至少保留一个段', true);
          return;
        }
        state.stack.segments.splice(segIndex, 1);
        syncParamsFromStack();
        renderStack();
        renderParams();
        draw();
      });
      head.appendChild(delSeg);
      host.appendChild(head);

      seg.layers.forEach(function (layer, layerIndex) {
        var row = document.createElement('div');
        row.className = 'stack-row';
        row.appendChild(makeTag('层 ' + (layerIndex + 1)));
        row.appendChild(makeText(layer.formula, function (v) {
          layer.formula = v;
          syncParamsFromStack();
          renderParams();
        }));
        row.appendChild(makeInput(layer.density, '0.01', function (v) {
          layer.density = v;
          syncParamValue('seg.' + segIndex + '.layer.' + layerIndex + '.density', v);
        }));
        row.appendChild(makeInput(layer.thickness, '0.1', function (v) {
          layer.thickness = v;
          syncParamValue('seg.' + segIndex + '.layer.' + layerIndex + '.thickness', v);
        }));
        row.appendChild(makeInput(layer.roughness, '0.1', function (v) {
          layer.roughness = v;
          syncParamValue('seg.' + segIndex + '.layer.' + layerIndex + '.roughness', v);
        }));
        var remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'btn-icon';
        remove.title = '删除该层';
        remove.textContent = '✕';
        remove.addEventListener('click', function () {
          if (seg.layers.length <= 1) {
            setStatus('每段至少保留一层', true);
            return;
          }
          seg.layers.splice(layerIndex, 1);
          syncParamsFromStack();
          renderStack();
          renderParams();
          draw();
        });
        row.appendChild(remove);
        host.appendChild(row);
      });

      var addLayer = document.createElement('button');
      addLayer.type = 'button';
      addLayer.className = 'btn btn-sm seg-add';
      addLayer.textContent = '＋ 层';
      addLayer.addEventListener('click', function () {
        seg.layers.push({ formula: 'Mo', density: 10.28, thickness: 20, roughness: 2 });
        syncParamsFromStack();
        renderStack();
        renderParams();
        draw();
      });
      host.appendChild(addLayer);
    });

    var sub = document.createElement('div');
    sub.className = 'stack-row is-substrate';
    sub.appendChild(makeTag('衬底'));
    sub.appendChild(makeText(state.stack.substrate.formula, function (v) { state.stack.substrate.formula = v; }));
    sub.appendChild(makeInput(state.stack.substrate.density, '0.001', function (v) {
      state.stack.substrate.density = v;
      syncParamValue('substrate.density', v);
    }));
    sub.appendChild(blank());
    sub.appendChild(makeInput(state.stack.substrate.roughness || 0, '0.1', function (v) {
      state.stack.substrate.roughness = v;
      syncParamValue('substrate.roughness', v);
    }));
    sub.appendChild(blank());
    host.appendChild(sub);

    updateLayerCount();
  }

  // ---------------------------------------------------------------- 参数表
  function findParam(key) {
    for (var i = 0; i < state.params.length; i++) {
      if (state.params[i].key === key) return state.params[i];
    }
    return null;
  }

  function bulkOf(formula) {
    if (!formula) return null;
    var key = String(formula).replace(/\s/g, '');
    if (state.bulk[key] != null) return state.bulk[key];
    var lower = key.toLowerCase();
    for (var name in state.bulk) {
      if (Object.prototype.hasOwnProperty.call(state.bulk, name) && name.toLowerCase() === lower) {
        return state.bulk[name];
      }
    }
    return null;
  }

  function loadMaterials() {
    if (!window.wgallop || !wgallop.backend) return;
    callBackend('materials', {}).then(function (res) {
      state.bulk = res.bulk || {};
      state.elements = res.elements || [];
      syncParamsFromStack();
      renderParams();
    }).catch(function () { /* 忽略：不影响主流程 */ });
  }

  function makeParam(key, label, value, defaultFit, defaultMin, defaultMax, extra) {
    var prev = findParam(key);
    if (prev) {
      prev.label = label;
      prev.value = value;
      if (extra) {
        prev.bulk = extra.bulk;
        prev.formula = extra.formula;
      }
      return prev;
    }
    return {
      key: key,
      label: label,
      value: value,
      fit: defaultFit,
      min: defaultMin,
      max: defaultMax,
      error: '',
      bulk: extra ? extra.bulk : null,
      formula: extra ? extra.formula : null
    };
  }

  function syncParamsFromStack() {
    var list = [];
    state.stack.segments.forEach(function (seg, segIndex) {
      seg.layers.forEach(function (layer, layerIndex) {
        var base = 'seg.' + segIndex + '.layer.' + layerIndex + '.';
        var prefix = '段' + (segIndex + 1) + '·层' + (layerIndex + 1) + ' ';
        var thick = numberOr(layer.thickness, 10);
        var rough = numberOr(layer.roughness, 2);
        var dens = numberOr(layer.density, 1);
        list.push(makeParam(base + 'thickness', prefix + '厚度',
          thick, true, Math.max(0.5, +(thick * 0.3).toFixed(2)), Math.max(1, +(thick * 2.5).toFixed(2))));
        list.push(makeParam(base + 'roughness', prefix + '界面宽度',
          rough, true, 0.1, Math.max(10, +(rough * 3).toFixed(1))));
        list.push(makeParam(base + 'density', prefix + '密度',
          dens, false, Math.max(0.1, +(dens * 0.6).toFixed(3)), +(dens * 1.4).toFixed(3),
          { bulk: bulkOf(layer.formula), formula: layer.formula }));
      });
    });

    var wl = numberOr(state.instrument.wavelength, 1.54);
    list.push(makeParam('inst.wavelength', '波长 λ (Å)', wl, false,
      +(wl * 0.95).toFixed(3), +(wl * 1.05).toFixed(3)));
    var resCfg = state.instrument.resolution || {};
    var resWidth = numberOr(resCfg.width, 0.001);
    list.push(makeParam('inst.res_width', '分辨率宽度 σ', resWidth, false, 0,
      Math.max(resWidth * 5, 0.01)));
    list.push(makeParam('inst.i0', 'I₀', numberOr(state.instrument.i0, 1), false, 0.05, 5));
    list.push(makeParam('inst.ibkg', '本底 I_bkg', numberOr(state.instrument.ibkg, 0), false, 0, 1e-3));
    state.params = list;
  }

  function syncParamValue(key, value) {
    var p = findParam(key);
    if (p) p.value = value;
  }

  function setStackValue(key, value) {
    var parts = key.split('.');
    if (parts[0] === 'seg' && parts.length >= 5) {
      var segIndex = parseInt(parts[1], 10);
      var layerIndex = parseInt(parts[3], 10);
      var segs = state.stack.segments;
      if (segs[segIndex] && segs[segIndex].layers[layerIndex]) {
        segs[segIndex].layers[layerIndex][parts[4]] = value;
      }
    } else if (parts[0] === 'layer' && parts.length === 3) {
      var index = parseInt(parts[1], 10);
      var flat = state.stack.segments[0] && state.stack.segments[0].layers;
      if (flat && flat[index]) flat[index][parts[2]] = value;
    } else if (parts[0] === 'inst') {
      state.instrument[parts[1]] = value;
      if (parts[1] === 'i0' && $('in-i0')) $('in-i0').value = String(value);
      if (parts[1] === 'ibkg' && $('in-ibkg')) $('in-ibkg').value = String(value);
    } else if (parts[0] === 'ambient' || parts[0] === 'substrate') {
      state.stack[parts[0]][parts[1]] = value;
    }
  }

  function renderParams() {
    var host = $('param-rows');
    if (!host) return;
    host.innerHTML = '';
    state.params.forEach(function (p) {
      var row = document.createElement('div');
      row.className = 'param-row';

      var name = document.createElement('span');
      name.className = 'p-name';
      name.textContent = p.label;
      row.appendChild(name);

      var valueInput = document.createElement('input');
      valueInput.type = 'number';
      valueInput.step = 'any';
      valueInput.value = String(p.value);
      valueInput.addEventListener('change', function () {
        var v = Number(valueInput.value);
        if (!isFinite(v)) { valueInput.classList.add('is-invalid'); return; }
        valueInput.classList.remove('is-invalid');
        p.value = v;
        setStackValue(p.key, v);
      });
      row.appendChild(valueInput);

      var fitBox = document.createElement('input');
      fitBox.type = 'checkbox';
      fitBox.className = 'p-fit-box';
      fitBox.checked = !!p.fit;
      fitBox.addEventListener('change', function () {
        p.fit = fitBox.checked;
        row.classList.toggle('is-free', p.fit);
      });
      var fitWrap = document.createElement('label');
      fitWrap.className = 'p-fit';
      fitWrap.appendChild(fitBox);
      row.appendChild(fitWrap);

      var minInput = document.createElement('input');
      minInput.type = 'number';
      minInput.step = 'any';
      minInput.value = String(p.min);
      minInput.addEventListener('change', function () {
        var v = Number(minInput.value);
        if (isFinite(v)) p.min = v;
      });
      row.appendChild(minInput);

      var maxInput = document.createElement('input');
      maxInput.type = 'number';
      maxInput.step = 'any';
      maxInput.value = String(p.max);
      maxInput.addEventListener('change', function () {
        var v = Number(maxInput.value);
        if (isFinite(v)) p.max = v;
      });
      row.appendChild(maxInput);

      var err = document.createElement('span');
      err.className = 'p-err';
      err.textContent = p.error || '—';
      row.appendChild(err);

      var note = document.createElement('span');
      note.className = 'p-note';
      if (p.bulk && p.bulk > 0) {
        var pct = (numberOr(p.value, 0) / p.bulk) * 100;
        note.textContent = 'bulk ' + p.bulk + ' · ' + pct.toFixed(1) + '%';
        note.title = '拟合密度 / 块体密度（' + p.formula + '）= ' + pct.toFixed(2) + '%';
      } else {
        note.textContent = '—';
      }
      row.appendChild(note);

      row.classList.toggle('is-free', !!p.fit);
      host.appendChild(row);
    });
  }

  // ---------------------------------------------------------------- 仿真
  function buildSimPayload() {
    var ds = activeDataset();
    var payload = {
      wavelength: numberOr(state.instrument.wavelength, 1.54),
      i0: numberOr(state.instrument.i0, 1),
      ibkg: numberOr(state.instrument.ibkg, 0),
      resolution: state.instrument.resolution,
      footprint: state.instrument.footprint,
      ambient: state.stack.ambient,
      substrate: state.stack.substrate,
      segments: state.stack.segments.map(function (seg) {
        return {
          repetitions: Math.max(1, Math.round(numberOr(seg.repetitions, 1))),
          layers: seg.layers.map(function (l) {
            return {
              formula: l.formula,
              density: numberOr(l.density, 0),
              thickness: numberOr(l.thickness, 0),
              roughness: numberOr(l.roughness, 0)
            };
          })
        };
      })
    };
    if (ds) {
      payload.x = ds.x;
      payload.x_unit = ds.xUnit;
    } else {
      payload.q = linspace(0.01, 0.6, 400);
      payload.x_unit = 'q';
    }
    return payload;
  }

  function linspace(a, b, n) {
    var out = new Array(n);
    for (var i = 0; i < n; i++) out[i] = a + (b - a) * i / (n - 1);
    return out;
  }

  function callBackend(method, payload) {
    if (!window.wgallop || !wgallop.backend) {
      return Promise.reject(new Error('未连接到宿主后端（请在 WGallop 中打开本插件）'));
    }
    return wgallop.backend.call(method, payload).then(function (res) {
      var data = res && res.data !== undefined ? res.data : res;
      if (res && res.ok === false) throw new Error(res.error || '后端返回失败');
      return data;
    });
  }

  function simulate() {
    if (state.busy) return;
    state.busy = true;
    var btn = $('btn-sim');
    btn.disabled = true;
    setStatus('仿真中…');
    var started = Date.now();
    var payload;
    try {
      payload = buildSimPayload();
    } catch (err) {
      state.busy = false;
      btn.disabled = false;
      setStatus('参数有误：' + err.message, true);
      return;
    }
    callBackend('simulate', payload).then(function (data) {
      if (!data || !data.intensity) throw new Error('后端没有返回曲线数据');
      state.sim = {
        q: data.q,
        intensity: data.intensity,
        reflectivity: data.reflectivity,
        x: payload.x || payload.q,
        xUnit: payload.x_unit
      };
      setStatus('仿真完成 · ' + data.intensity.length + ' 点 · ' + (Date.now() - started) + ' ms');
      draw();
    }).catch(function (err) {
      setStatus('仿真失败：' + (err && err.message ? err.message : err), true);
    }).then(function () {
      state.busy = false;
      btn.disabled = false;
    });
  }

  // ---------------------------------------------------------------- 拟合
  function algorithmSettings() {
    return {
      fom: $('in-fom') ? $('in-fom').value : 'chi2bars',
      max_generations: Math.max(1, Math.round(numberOr($('in-maxgen') && $('in-maxgen').value, 200))),
      population: Math.max(0, Math.round(numberOr($('in-pop') && $('in-pop').value, 0))),
      f: numberOr($('in-f') && $('in-f').value, 0.7),
      cr: numberOr($('in-cr') && $('in-cr').value, 0.7),
      strategy: $('in-strategy') ? $('in-strategy').value : 'best_1_bin',
      spread_tol: numberOr($('in-spread') && $('in-spread').value, 0)
    };
  }

  function startFit() {
    if (state.fitRunning) return;
    var ds = activeDataset();
    if (!ds) { setStatus('请先导入一条数据再拟合', true); return; }
    var free = state.params.filter(function (p) { return p.fit; });
    if (!free.length) { setStatus('请至少勾选一个参与拟合的参数', true); return; }
    for (var i = 0; i < free.length; i++) {
      if (!(free[i].max > free[i].min)) {
        setStatus('参数「' + free[i].label + '」的最大值必须大于最小值', true);
        return;
      }
    }

    state.fitRunning = true;
    state.fitHistory = [];
    state.fitGen = 0;
    state.fitBest = null;
    $('btn-fit').disabled = true;
    $('btn-stop').disabled = false;
    state.plotMode = 'fom';
    syncPlotTabs();
    setStatus('拟合已启动…');

    var payload = {
      stack: state.stack,
      instrument: {
        wavelength: numberOr(state.instrument.wavelength, 1.54),
        i0: numberOr(state.instrument.i0, 1),
        ibkg: numberOr(state.instrument.ibkg, 0),
        resolution: state.instrument.resolution,
        footprint: state.instrument.footprint
      },
      data: { x: ds.x, y: ds.y, error: ds.error, x_unit: ds.xUnit },
      params: state.params.map(function (p) {
        return { key: p.key, label: p.label, value: p.value, fit: p.fit, min: p.min, max: p.max };
      }),
      algorithm: algorithmSettings()
    };

    callBackend('fit_start', payload).then(function (data) {
      state.fitJob = data.job_id;
      state.fitMaxGen = data.max_generations;
      pollFit();
    }).catch(function (err) {
      state.fitRunning = false;
      $('btn-fit').disabled = false;
      $('btn-stop').disabled = true;
      setStatus('拟合启动失败：' + (err && err.message ? err.message : err), true);
    });
  }

  function pollFit() {
    if (!state.fitJob) return;
    callBackend('fit_status', { job_id: state.fitJob }).then(function (snap) {
      state.fitGen = snap.generation;
      state.fitMaxGen = snap.max_generations;
      state.fitBest = snap.best_fom;
      state.fitHistory = snap.history || [];
      $('plot-meta').textContent = '第 ' + snap.generation + ' / ' + snap.max_generations +
        ' 代 · FOM ' + fmt(snap.best_fom);
      draw();
      if (snap.state === 'running') {
        state.fitTimer = setTimeout(pollFit, 350);
      } else {
        finishFit(snap);
      }
    }).catch(function (err) {
      state.fitRunning = false;
      $('btn-fit').disabled = false;
      $('btn-stop').disabled = true;
      setStatus('轮询失败：' + (err && err.message ? err.message : err), true);
    });
  }

  function finishFit(snap) {
    state.fitRunning = false;
    $('btn-fit').disabled = false;
    $('btn-stop').disabled = true;
    state.fitTimer = null;

    if (snap.state === 'error') {
      setStatus('拟合失败：' + snap.error, true);
      draw();
      return;
    }

    if (snap.final_params) {
      Object.keys(snap.final_params).forEach(function (key) {
        var value = snap.final_params[key];
        setStackValue(key, value);
        var p = findParam(key);
        if (p) p.value = value;
      });
      if (snap.errors) {
        Object.keys(snap.errors).forEach(function (key) {
          var pe = findParam(key);
          if (pe) pe.error = '± ' + fmt(snap.errors[key]);
        });
      }
      renderStack();
      renderParams();
    }

    if (snap.final_intensity) {
      var ds = activeDataset();
      state.sim = {
        q: null,
        intensity: snap.final_intensity,
        x: ds ? ds.x : linspace(0.01, 0.6, snap.final_intensity.length),
        xUnit: ds ? ds.xUnit : 'q'
      };
    }

    var elapsed = snap.elapsed ? snap.elapsed.toFixed(1) + ' s' : '';
    setStatus('拟合' + (snap.state === 'stopped' ? '已停止' : '完成') +
      ' · ' + snap.generation + ' 代 · FOM ' + fmt(snap.best_fom) + (elapsed ? ' · ' + elapsed : ''));
    draw();
  }

  function stopFit() {
    if (!state.fitRunning || !state.fitJob) return;
    $('btn-stop').disabled = true;
    setStatus('正在停止…');
    callBackend('fit_stop', { job_id: state.fitJob }).catch(function () { /* 忽略 */ });
  }

  // ---------------------------------------------------------------- 绘图
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function prepareCanvas() {
    var canvas = $('plot');
    var wrap = canvas.parentElement;
    var width = wrap.clientWidth;
    var height = wrap.clientHeight;
    if (width < 10 || height < 10) return null;
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    return { ctx: ctx, width: width, height: height };
  }

  function draw() {
    var env = prepareCanvas();
    if (!env) return;
    if (state.plotMode === 'fom') drawFom(env);
    else drawData(env);
  }

  function drawData(env) {
    var ctx = env.ctx;
    var width = env.width;
    var height = env.height;
    var ds = activeDataset();
    var sim = state.sim;
    var hasData = !!ds;
    var hasSim = !!(sim && sim.intensity && sim.intensity.length);

    $('plot-empty').classList.toggle('is-hidden', hasData || hasSim);
    if (!hasData && !hasSim) return;

    var pad = { l: 62, r: 18, t: 16, b: 36 };
    var W = width - pad.l - pad.r;
    var H = height - pad.t - pad.b;
    if (W <= 10 || H <= 10) return;

    var xs = [];
    var ys = [];
    if (hasData) {
      for (var i = 0; i < ds.x.length; i++) { xs.push(ds.x[i]); ys.push(ds.y[i]); }
    }
    if (hasSim) {
      for (var j = 0; j < sim.x.length; j++) { xs.push(sim.x[j]); ys.push(sim.intensity[j]); }
    }

    var xMin = Math.min.apply(null, xs);
    var xMax = Math.max.apply(null, xs);
    if (!(xMax > xMin)) xMax = xMin + 1;

    var positives = ys.filter(function (v) { return v > 0 && isFinite(v); });
    if (!positives.length) positives = [1];
    var yMinLog = Math.log10(Math.min.apply(null, positives));
    var yMaxLog = Math.log10(Math.max.apply(null, positives));
    if (yMaxLog - yMinLog < 1) yMaxLog = yMinLog + 1;
    yMinLog = Math.floor(yMinLog - 0.15);
    yMaxLog = Math.ceil(yMaxLog + 0.15);

    var sx = function (x) { return pad.l + (x - xMin) / (xMax - xMin) * W; };
    var sy = function (y) {
      var v = y > 0 ? Math.log10(y) : yMinLog;
      return pad.t + (yMaxLog - v) / (yMaxLog - yMinLog) * H;
    };

    var border = cssVar('--wg-border') || '#E4E4E4';
    var textColor = cssVar('--wg-text-secondary') || '#6B6B6B';
    var dataColor = cssVar('--xrr-data') || '#1F6FEB';
    var simColor = cssVar('--xrr-sim') || '#E4572E';

    ctx.strokeStyle = border;
    ctx.lineWidth = 1;
    ctx.font = '11px "Microsoft YaHei", system-ui, sans-serif';
    ctx.fillStyle = textColor;

    var yTicks = 5;
    for (var k = 0; k <= yTicks; k++) {
      var yv = yMinLog + (yMaxLog - yMinLog) * k / yTicks;
      var py = pad.t + H - (H * k / yTicks);
      ctx.beginPath();
      ctx.moveTo(pad.l, py + 0.5);
      ctx.lineTo(pad.l + W, py + 0.5);
      ctx.stroke();
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText('1e' + yv.toFixed(0), pad.l - 8, py);
    }
    var xTicks = 6;
    for (var m = 0; m <= xTicks; m++) {
      var xv = xMin + (xMax - xMin) * m / xTicks;
      var px = pad.l + W * m / xTicks;
      ctx.beginPath();
      ctx.moveTo(px + 0.5, pad.t);
      ctx.lineTo(px + 0.5, pad.t + H);
      ctx.stroke();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(xv.toFixed(3), px, pad.t + H + 8);
    }

    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.fillText(unitAxisLabel(ds ? ds.xUnit : 'q'), pad.l + W / 2, height - 2);
    ctx.save();
    ctx.translate(14, pad.t + H / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textBaseline = 'top';
    ctx.fillText('强度', 0, 0);
    ctx.restore();

    if (hasData) {
      ctx.fillStyle = dataColor;
      for (var n = 0; n < ds.x.length; n++) {
        var vx = ds.x[n];
        var vy = ds.y[n];
        if (!isFinite(vx) || !isFinite(vy)) continue;
        var cx = sx(vx);
        var cy = sy(vy);
        if (cx < pad.l - 2 || cx > pad.l + W + 2) continue;
        ctx.fillRect(cx - 1, cy - 1, 2, 2);
      }
    }

    if (hasSim) {
      ctx.strokeStyle = simColor;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      var started = false;
      for (var p = 0; p < sim.x.length; p++) {
        var sxv = sim.x[p];
        var syv = sim.intensity[p];
        if (!isFinite(sxv) || !isFinite(syv)) { started = false; continue; }
        var pxp = sx(sxv);
        var pyp = sy(syv);
        if (!started) { ctx.moveTo(pxp, pyp); started = true; }
        else ctx.lineTo(pxp, pyp);
      }
      ctx.stroke();
    }

    var legendY = pad.t + 8;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    if (hasData) {
      ctx.fillStyle = dataColor;
      ctx.fillRect(pad.l + 8, legendY - 3, 10, 3);
      ctx.fillStyle = textColor;
      ctx.fillText('数据', pad.l + 24, legendY);
      legendY += 16;
    }
    if (hasSim) {
      ctx.fillStyle = simColor;
      ctx.fillRect(pad.l + 8, legendY - 3, 10, 3);
      ctx.fillStyle = textColor;
      ctx.fillText('模拟', pad.l + 24, legendY);
    }
  }

  function drawFom(env) {
    var ctx = env.ctx;
    var width = env.width;
    var height = env.height;
    var history = state.fitHistory;
    $('plot-empty').classList.toggle('is-hidden', !!(history && history.length));

    if (!history || !history.length) {
      $('plot-meta').textContent = '';
      return;
    }

    var pad = { l: 70, r: 18, t: 18, b: 36 };
    var W = width - pad.l - pad.r;
    var H = height - pad.t - pad.b;
    if (W <= 10 || H <= 10) return;

    var vals = history.filter(function (v) { return isFinite(v) && v > 0; });
    if (!vals.length) vals = [1];
    var yMinLog = Math.log10(Math.min.apply(null, vals));
    var yMaxLog = Math.log10(Math.max.apply(null, vals));
    if (yMaxLog - yMinLog < 0.5) { yMinLog -= 0.25; yMaxLog += 0.25; }
    yMinLog = Math.floor(yMinLog * 4) / 4;
    yMaxLog = Math.ceil(yMaxLog * 4) / 4;

    var n = history.length;
    var xMax = Math.max(n - 1, 1);
    var sx = function (i) { return pad.l + i / xMax * W; };
    var sy = function (v) {
      var lv = v > 0 ? Math.log10(v) : yMinLog;
      return pad.t + (yMaxLog - lv) / (yMaxLog - yMinLog) * H;
    };

    var border = cssVar('--wg-border') || '#E4E4E4';
    var textColor = cssVar('--wg-text-secondary') || '#6B6B6B';
    var accent = cssVar('--wg-accent') || '#4176E6';

    ctx.strokeStyle = border;
    ctx.lineWidth = 1;
    ctx.font = '11px "Microsoft YaHei", system-ui, sans-serif';
    ctx.fillStyle = textColor;

    var yTicks = 5;
    for (var k = 0; k <= yTicks; k++) {
      var yv = yMinLog + (yMaxLog - yMinLog) * k / yTicks;
      var py = pad.t + H - (H * k / yTicks);
      ctx.beginPath();
      ctx.moveTo(pad.l, py + 0.5);
      ctx.lineTo(pad.l + W, py + 0.5);
      ctx.stroke();
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText('1e' + yv.toFixed(1), pad.l - 8, py);
    }
    var xTicks = Math.min(8, Math.max(2, n - 1));
    for (var m = 0; m <= xTicks; m++) {
      var idx = Math.round((n - 1) * m / xTicks);
      var px = sx(idx);
      ctx.beginPath();
      ctx.moveTo(px + 0.5, pad.t);
      ctx.lineTo(px + 0.5, pad.t + H);
      ctx.stroke();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(String(idx), px, pad.t + H + 8);
    }

    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.fillText('代数', pad.l + W / 2, height - 2);
    ctx.save();
    ctx.translate(16, pad.t + H / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textBaseline = 'top';
    ctx.fillText('FOM（对数）', 0, 0);
    ctx.restore();

    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    for (var i = 0; i < n; i++) {
      var v = history[i];
      if (!isFinite(v) || v <= 0) continue;
      var pxp = sx(i);
      var pyp = sy(v);
      if (i === 0) ctx.moveTo(pxp, pyp);
      else ctx.lineTo(pxp, pyp);
    }
    ctx.stroke();

    var best = history[n - 1];
    ctx.fillStyle = accent;
    ctx.beginPath();
    ctx.arc(sx(n - 1), sy(best), 3, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = textColor;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText('当前最优 FOM = ' + fmt(best), pad.l + 10, pad.t + 12);
  }

  function syncPlotTabs() {
    document.querySelectorAll('.plot-tab').forEach(function (tab) {
      tab.classList.toggle('is-active', tab.getAttribute('data-plot') === state.plotMode);
    });
  }

  // ---------------------------------------------------------------- 项目与导出
  var modalOk = null;

  function openModal(title, bodyNode, onOk, okLabel) {
    $('modal-title').textContent = title;
    var body = $('modal-body');
    body.innerHTML = '';
    if (bodyNode) body.appendChild(bodyNode);
    $('modal-ok').textContent = okLabel || '确定';
    $('modal-ok').disabled = false;
    modalOk = onOk;
    $('modal').classList.remove('is-hidden');
  }

  function closeModal() {
    $('modal').classList.add('is-hidden');
    $('modal-cancel').style.display = '';
    modalOk = null;
  }

  function syncInstrumentControls() {
    var inst = state.instrument;
    $('in-wavelength').value = String(inst.wavelength);
    $('in-i0').value = String(inst.i0);
    $('in-ibkg').value = String(inst.ibkg);
    var res = inst.resolution || {};
    $('in-restype').value = res.type || 'none';
    $('in-res').value = String(res.width != null ? res.width : 0.001);
    $('in-respoints').value = String(res.points != null ? res.points : 5);
    $('in-resspread').value = String(res.spread != null ? res.spread : 2);
    var foot = inst.footprint || {};
    $('in-footype').value = foot.type || 'none';
    $('in-beamw').value = String(foot.beam_width != null ? foot.beam_width : 0.05);
    $('in-samplelen').value = String(foot.sample_length != null ? foot.sample_length : 10);
  }

  function collectProject() {
    return {
      version: 4,
      savedAt: new Date().toISOString(),
      stack: state.stack,
      instrument: state.instrument,
      params: state.params,
      datasets: state.datasets,
      activeId: state.activeId,
      algorithm: algorithmSettings(),
      fit: state.fitHistory && state.fitHistory.length
        ? { history: state.fitHistory, best: state.fitBest, generation: state.fitGen }
        : null
    };
  }

  function applyProject(data) {
    if (data.stack && data.stack.segments) state.stack = data.stack;
    if (data.instrument) {
      state.instrument = data.instrument;
      if (!state.instrument.resolution) {
        state.instrument.resolution = { type: 'none', width: 0.001, points: 5, spread: 2 };
      }
      if (!state.instrument.footprint) {
        state.instrument.footprint = { type: 'none', beam_width: 0.05, sample_length: 10 };
      }
    }
    if (Array.isArray(data.datasets)) {
      state.datasets = data.datasets;
      state.seq = state.datasets.length;
      state.activeId = data.activeId || (state.datasets[0] && state.datasets[0].id) || null;
    }
    if (Array.isArray(data.params) && data.params.length) {
      state.params = data.params;
    } else {
      syncParamsFromStack();
    }
    if (data.fit && Array.isArray(data.fit.history)) {
      state.fitHistory = data.fit.history;
      state.fitBest = data.fit.best;
      state.fitGen = data.fit.generation || data.fit.history.length - 1;
    }
    if (data.algorithm && data.algorithm.fom && $('in-fom')) {
      $('in-fom').value = data.algorithm.fom;
    }
    syncInstrumentControls();
    renderDataList();
    renderStack();
    renderParams();
    draw();
  }

  function readFileAsBase64(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        var bytes = new Uint8Array(reader.result);
        var binary = '';
        var chunk = 0x8000;
        for (var i = 0; i < bytes.length; i += chunk) {
          binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        resolve(btoa(binary));
      };
      reader.onerror = function () { reject(new Error('读取文件失败')); };
      reader.readAsArrayBuffer(file);
    });
  }

  function importProjectFile(file) {
    if (!file) return;
    var name = file.name || '';
    if (/\.json$/i.test(name)) {
      var reader = new FileReader();
      reader.onload = function () {
        try {
          var data = JSON.parse(String(reader.result || '{}'));
          applyProject(data);
          setStatus('已打开工程：' + name);
        } catch (err) {
          setStatus('工程文件解析失败：' + err.message, true);
        }
      };
      reader.onerror = function () { setStatus('读取工程失败', true); };
      reader.readAsText(file, 'utf-8');
      return;
    }
    setStatus('正在解析 ' + name + ' …');
    readFileAsBase64(file).then(function (b64) {
      return callBackend('import_hgx', { data: b64, name: name });
    }).then(function (data) {
      applyProject(data);
      var meta = data.meta || {};
      setStatus('已导入 ' + (meta.total_layers || 0) + ' 层（' + (meta.segment_count || 0) +
        ' 段）· ' + (meta.params || 0) + ' 个参数');
    }).catch(function (err) {
      setStatus('导入 .hgx 失败：' + (err && err.message ? err.message : err), true);
    });
  }

  function showDiagnose() {
    var body = document.createElement('div');
    var pre = document.createElement('pre');
    pre.style.whiteSpace = 'pre-wrap';
    pre.style.fontFamily = "'JetBrains Mono', Consolas, monospace";
    pre.style.fontSize = '12px';
    pre.textContent = '正在检查…';
    body.appendChild(pre);
    openModal('后端环境自检', body, null, '关闭');
    $('modal-cancel').style.display = 'none';

    callBackend('diagnose', {}).then(function (info) {
      var lines = [];
      lines.push('Python      : ' + info.python);
      lines.push('解释器      : ' + info.executable);
      lines.push('numpy       : ' + info.numpy);
      lines.push('h5py        : ' + info.h5py);
      lines.push('scipy       : ' + info.scipy);
      lines.push('vendor 目录 : ' + info.vendor);
      lines.push('vendor 存在 : ' + info.vendor_exists);
      lines.push('vendor 已注入: ' + info.vendor_in_path);
      lines.push('元素表      : ' + info.elements + ' 个');
      lines.push('');
      lines.push('sys.path 前几项：');
      (info.sys_path || []).forEach(function (p) { lines.push('  ' + p); });
      pre.textContent = lines.join('\n');
    }).catch(function (err) {
      pre.textContent = '自检失败：' + (err && err.message ? err.message : err) +
        '\n\n说明后端进程没有响应，通常是未授权或解释器缺少依赖。';
    });
  }

  function saveProject() {
    var body = document.createElement('div');
    var label = document.createElement('label');
    label.className = 'field';
    var span = document.createElement('span');
    span.textContent = '项目名称';
    var input = document.createElement('input');
    input.type = 'text';
    input.id = 'proj-name';
    input.value = 'xrr-' + new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    label.appendChild(span);
    label.appendChild(input);
    body.appendChild(label);
    var hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = '保存为 JSON 项目文件，默认目录 ~/Documents/XRR。';
    body.appendChild(hint);

    openModal('保存项目', body, function () {
      var name = input.value.trim();
      callBackend('save_project', { name: name, data: collectProject() }).then(function (res) {
        closeModal();
        setStatus('已保存：' + res.path);
      }).catch(function (err) {
        setStatus('保存失败：' + (err && err.message ? err.message : err), true);
      });
    }, '保存');
  }

  function openProject() {
    var body = document.createElement('div');

    var listTitle = document.createElement('div');
    listTitle.className = 'hint';
    listTitle.textContent = '最近项目';
    body.appendChild(listTitle);

    var list = document.createElement('ul');
    list.className = 'proj-list';
    body.appendChild(list);

    var hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = '正在读取最近项目…';
    body.appendChild(hint);

    var label = document.createElement('label');
    label.className = 'field';
    var span = document.createElement('span');
    span.textContent = '或直接输入文件路径（.json 工程 / .hgx GenX 项目）';
    var pathInput = document.createElement('input');
    pathInput.type = 'text';
    pathInput.id = 'proj-path';
    pathInput.placeholder = 'D:\\genx_plugin\\GenX拟合实例\\实例1-NiVTi多层膜\\NiVTi-N20-fit model.hgx';
    label.appendChild(span);
    label.appendChild(pathInput);
    body.appendChild(label);

    var selected = null;

    openModal('打开工程', body, function () {
      var target = pathInput.value.trim() || selected;
      if (!target) {
        setStatus('请选择最近项目或输入文件路径', true);
        return;
      }
      var isHgx = /\.hgx$/i.test(target);
      var call = isHgx
        ? callBackend('import_hgx', { path: target })
        : callBackend('load_project', { path: target });
      call.then(function (out) {
        var data = isHgx ? out : out.data;
        applyProject(data);
        closeModal();
        if (isHgx && data.meta) {
          setStatus('已导入 ' + data.meta.total_layers + ' 层（' + data.meta.segment_count +
            ' 段）· ' + data.meta.params + ' 个参数');
        } else {
          setStatus('已打开：' + out.path);
        }
      }).catch(function (err) {
        setStatus('打开失败：' + (err && err.message ? err.message : err), true);
      });
    }, '打开');

    callBackend('list_projects', {}).then(function (res) {
      hint.textContent = (res.files && res.files.length)
        ? '默认目录：' + res.dir
        : '默认目录还没有项目：' + res.dir;
      if (!res.files || !res.files.length) {
        return;
      }
      res.files.forEach(function (file) {
        var li = document.createElement('li');
        li.className = 'proj-item';
        var name = document.createElement('span');
        name.className = 'pname';
        name.textContent = file.name;
        var meta = document.createElement('span');
        meta.className = 'pmeta';
        meta.textContent = file.mtime + ' · ' + Math.round(file.size / 1024) + ' KB';
        li.appendChild(name);
        li.appendChild(meta);
        li.addEventListener('click', function () {
          Array.prototype.forEach.call(list.children, function (c) {
            c.classList.remove('is-active');
          });
          li.classList.add('is-active');
          selected = file.path;
          pathInput.value = file.path;
        });
        list.appendChild(li);
      });
    }).catch(function (err) {
      hint.textContent = '无法读取最近项目（' + (err && err.message ? err.message : err) +
        '）。可以直接在上面输入完整路径。';
    });
  }

  function buildCurveRows() {
    var ds = activeDataset();
    var sim = state.sim;
    var rows = [['x', 'measured', 'simulated', 'residual']];
    if (!ds) return rows;
    var simMap = null;
    if (sim && sim.intensity && sim.x && sim.intensity.length === sim.x.length) {
      simMap = {};
      for (var k = 0; k < sim.x.length; k += 1) simMap[sim.x[k]] = sim.intensity[k];
    }
    for (var i = 0; i < ds.x.length; i += 1) {
      var xv = ds.x[i];
      var yv = ds.y[i];
      var sv = sim && sim.intensity && sim.intensity.length === ds.x.length
        ? sim.intensity[i] : (simMap && simMap[xv] != null ? simMap[xv] : '');
      var rv = (sv === '' || sv == null) ? '' : yv - sv;
      rows.push([xv, yv, sv, rv]);
    }
    return rows;
  }

  function fmtNum(value, digits) {
    if (value === null || value === undefined || !isFinite(value)) return '';
    var n = Number(value);
    if (n !== 0 && (Math.abs(n) >= 1e4 || Math.abs(n) < 1e-3)) return n.toExponential(4);
    return n.toFixed(digits == null ? 4 : digits);
  }

  function densityNote(density, formula) {
    var bulk = bulkOf(formula);
    if (!bulk || bulk <= 0) return ['', ''];
    return [String(bulk), ((numberOr(density, 0) / bulk) * 100).toFixed(1)];
  }

  function buildStructureRows() {
    var rows = [['层', '材料', '厚度(A)', '界面宽度(A)', '密度(g/cm3)',
                 'bulk(g/cm3)', '密度占bulk(%)']];
    var amb = state.stack.ambient;
    var ambNote = densityNote(numberOr(amb.density, 0), amb.formula);
    rows.push(['环境', amb.formula, '', '', fmtNum(amb.density), ambNote[0], ambNote[1]]);
    state.stack.segments.forEach(function (seg, si) {
      seg.layers.forEach(function (layer, li) {
        var note = densityNote(numberOr(layer.density, 0), layer.formula);
        rows.push([
          '段' + (si + 1) + '-层' + (li + 1) + 'x' + seg.repetitions,
          layer.formula,
          fmtNum(layer.thickness),
          fmtNum(layer.roughness),
          fmtNum(layer.density),
          note[0],
          note[1]
        ]);
      });
    });
    var sub = state.stack.substrate;
    var subNote = densityNote(numberOr(sub.density, 0), sub.formula);
    rows.push(['衬底', sub.formula, '', fmtNum(sub.roughness || 0),
      fmtNum(sub.density), subNote[0], subNote[1]]);
    return rows;
  }

  function buildParamRows() {
    var rows = [['parameter', 'value', 'fit', 'min', 'max', 'error']];
    state.params.forEach(function (p) {
      rows.push([p.label, p.value, p.fit, p.min, p.max, p.error || '']);
    });
    return rows;
  }

  function buildSummaryRows() {
    var ds = activeDataset();
    var lines = [];
    lines.push('XRR 拟合工作台 结果摘要');
    lines.push('导出时间: ' + new Date().toLocaleString());
    lines.push('');
    lines.push('[仪器]');
    lines.push('  波长 (A): ' + state.instrument.wavelength);
    lines.push('  I0: ' + state.instrument.i0);
    lines.push('  本底: ' + state.instrument.ibkg);
    var res = state.instrument.resolution || {};
    lines.push('  分辨率: ' + (res.type || 'none') +
      (res.type && res.type !== 'none' ? '  sigma=' + res.width + ' points=' + res.points : ''));
    var foot = state.instrument.footprint || {};
    lines.push('  照射面积: ' + (foot.type || 'none') +
      (foot.type && foot.type !== 'none' ? '  beam=' + foot.beam_width + 'mm sample=' + foot.sample_length + 'mm' : ''));
    lines.push('');
    lines.push('[层结构]');
    lines.push('  环境: ' + state.stack.ambient.formula + '  ' + state.stack.ambient.density + ' g/cm3');
    state.stack.segments.forEach(function (seg, si) {
      lines.push('  段 ' + (si + 1) + '  重复 ' + seg.repetitions + ' 次');
      seg.layers.forEach(function (layer, li) {
        lines.push('    ' + (li + 1) + '. ' + layer.formula +
          '  d=' + layer.thickness + ' A' +
          '  rho=' + layer.density + ' g/cm3' +
          '  sigma=' + layer.roughness + ' A');
      });
    });
    lines.push('  衬底: ' + state.stack.substrate.formula + '  ' + state.stack.substrate.density + ' g/cm3');
    lines.push('');
    lines.push('[数据]');
    if (ds) {
      lines.push('  ' + ds.name + '  ' + ds.x.length + ' 点  ' + (ds.xUnit === 'q' ? 'q' : '2theta'));
    } else {
      lines.push('  （无）');
    }
    lines.push('');
    lines.push('[拟合]');
    lines.push('  FOM: ' + (state.fitBest == null ? '未拟合' : state.fitBest));
    lines.push('  代数: ' + state.fitGen);
    lines.push('');
    lines.push('[参数]');
    lines.push('  ' + ['名称', '值', '拟合', '最小', '最大', '误差'].join('\t'));
    state.params.forEach(function (p) {
      lines.push('  ' + [p.label, p.value, p.fit ? 'yes' : 'no', p.min, p.max, p.error || '-'].join('\t'));
    });
    return lines;
  }

  function exportDialog() {
    var body = document.createElement('div');
    var group = document.createElement('div');
    group.className = 'radio-row';
    var options = [
      ['curve', '拟合曲线 .dat（实测 / 拟合 / 残差）', 'dat'],
      ['curve_csv', '拟合曲线 .csv', 'csv'],
      ['structure', '膜层结构参数表（含密度占 bulk %）', 'csv'],
      ['params', '拟合参数表（值 / 边界 / 误差）', 'csv'],
      ['summary', '结果摘要（文本）', 'txt']
    ];
    var chosen = 'curve';
    options.forEach(function (opt, index) {
      var label = document.createElement('label');
      var radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'export-kind';
      radio.value = opt[0];
      radio.checked = index === 0;
      radio.addEventListener('change', function () { chosen = opt[0]; });
      var text = document.createElement('span');
      text.textContent = opt[1];
      label.appendChild(radio);
      label.appendChild(text);
      group.appendChild(label);
    });
    body.appendChild(group);

    var nameLabel = document.createElement('label');
    nameLabel.className = 'field';
    var span = document.createElement('span');
    span.textContent = '文件名（不含扩展名）';
    var nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.id = 'export-name';
    nameInput.value = 'xrr-' + new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    nameLabel.appendChild(span);
    nameLabel.appendChild(nameInput);
    body.appendChild(nameLabel);

    var hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = '导出到 ~/Documents/XRR。';
    body.appendChild(hint);

    openModal('导出', body, function () {
      var rows;
      var ext = 'csv';
      var kind = chosen;
      if (chosen === 'curve' || chosen === 'curve_csv') {
        rows = buildCurveRows();
        ext = chosen === 'curve' ? 'dat' : 'csv';
        kind = 'curve';
        if (rows.length <= 1) {
          setStatus('没有可导出的曲线（先导入数据并仿真）', true);
          return;
        }
      } else if (chosen === 'structure') {
        rows = buildStructureRows();
      } else if (chosen === 'params') {
        rows = buildParamRows();
      } else {
        rows = buildSummaryRows();
        ext = 'txt';
      }
      callBackend('export_data', {
        kind: kind,
        name: nameInput.value.trim(),
        ext: ext,
        rows: rows
      }).then(function (res) {
        closeModal();
        setStatus('已导出 ' + res.rows + ' 行：' + res.path);
      }).catch(function (err) {
        setStatus('导出失败：' + (err && err.message ? err.message : err), true);
      });
    }, '导出');
  }

  // ---------------------------------------------------------------- 绑定
  function bind() {
    $('btn-open').addEventListener('click', function () { $('file-input').click(); });
    $('btn-import').addEventListener('click', function () { $('file-input').click(); });
    $('file-input').addEventListener('change', function (ev) {
      importFiles(ev.target.files);
      ev.target.value = '';
    });

    $('btn-sim').addEventListener('click', simulate);
    $('btn-save').addEventListener('click', saveProject);
    $('btn-load').addEventListener('click', function () { $('project-file').click(); });
    $('project-file').addEventListener('change', function (ev) {
      var file = ev.target.files && ev.target.files[0];
      importProjectFile(file);
      ev.target.value = '';
    });
    var linkPath = $('link-manual-path');
    if (linkPath) {
      linkPath.addEventListener('click', function (ev) {
        ev.preventDefault();
        openProject();
      });
    }
    var linkDiag = $('link-diagnose');
    if (linkDiag) {
      linkDiag.addEventListener('click', function (ev) {
        ev.preventDefault();
        showDiagnose();
      });
    }
    $('btn-export').addEventListener('click', exportDialog);
    $('modal-close').addEventListener('click', closeModal);
    $('modal-cancel').addEventListener('click', closeModal);
    $('modal-ok').addEventListener('click', function () {
      if (typeof modalOk === 'function') modalOk();
    });
    $('modal').addEventListener('click', function (ev) {
      if (ev.target === $('modal')) closeModal();
    });
    $('btn-fit').addEventListener('click', startFit);
    $('btn-stop').addEventListener('click', stopFit);

    $('btn-remove').addEventListener('click', function () {
      if (!state.activeId) return;
      state.datasets = state.datasets.filter(function (d) { return d.id !== state.activeId; });
      state.activeId = state.datasets.length ? state.datasets[0].id : null;
      renderDataList();
      draw();
      setStatus(state.datasets.length ? '已删除选中数据集' : '数据已清空');
    });

    $('btn-reset').addEventListener('click', function () {
      if (state.fitRunning) stopFit();
      state.datasets = [];
      state.activeId = null;
      state.sim = null;
      state.fitHistory = [];
      state.fitGen = 0;
      state.fitBest = null;
      state.stack = {
        ambient: { formula: 'Air', density: 0.0012 },
        segments: [
          {
            repetitions: 1,
            layers: [
              { formula: 'Mo', density: 10.28, thickness: 28, roughness: 3 },
              { formula: 'Si', density: 2.33, thickness: 42, roughness: 4 }
            ]
          }
        ],
        substrate: { formula: 'Si', density: 2.329 }
      };
      state.instrument = {
        wavelength: 1.54, i0: 1, ibkg: 0,
        resolution: { type: 'none', width: 0.001, points: 5, spread: 2 },
        footprint: { type: 'none', beam_width: 0.05, sample_length: 10 }
      };
      $('in-wavelength').value = '1.54';
      $('in-i0').value = '1';
      $('in-ibkg').value = '0';
      $('in-restype').value = 'none';
      $('in-res').value = '0.001';
      $('in-respoints').value = '5';
      $('in-resspread').value = '2';
      $('in-footype').value = 'none';
      $('in-beamw').value = '0.05';
      $('in-samplelen').value = '10';
      syncParamsFromStack();
      renderDataList();
      renderStack();
      renderParams();
      draw();
      setStatus('已重置');
    });

    $('btn-add-layer').addEventListener('click', function () {
      var seg = state.stack.segments[state.stack.segments.length - 1];
      seg.layers.push({ formula: 'Mo', density: 10.28, thickness: 20, roughness: 2 });
      syncParamsFromStack();
      renderStack();
      renderParams();
      draw();
    });

    $('btn-add-pair').addEventListener('click', function () {
      var seg = state.stack.segments[state.stack.segments.length - 1];
      seg.layers.push({ formula: 'Mo', density: 10.28, thickness: 28, roughness: 3 });
      seg.layers.push({ formula: 'Si', density: 2.33, thickness: 42, roughness: 4 });
      syncParamsFromStack();
      renderStack();
      renderParams();
      draw();
      setStatus('已在最后一段追加一层 Mo/Si');
    });

    var addSeg = $('btn-add-seg');
    if (addSeg) {
      addSeg.addEventListener('click', function () {
        state.stack.segments.push({
          repetitions: 1,
          layers: [{ formula: 'Mo', density: 10.28, thickness: 20, roughness: 2 }]
        });
        syncParamsFromStack();
        renderStack();
        renderParams();
        draw();
      });
    }

    document.querySelectorAll('.tab').forEach(function (tab) {
      tab.addEventListener('click', function () {
        document.querySelectorAll('.tab').forEach(function (t) { t.classList.remove('is-active'); });
        document.querySelectorAll('.tab-panel').forEach(function (p) { p.classList.add('is-hidden'); });
        tab.classList.add('is-active');
        var panel = $('panel-' + tab.getAttribute('data-tab'));
        if (panel) panel.classList.remove('is-hidden');
      });
    });

    document.querySelectorAll('.plot-tab').forEach(function (tab) {
      tab.addEventListener('click', function () {
        state.plotMode = tab.getAttribute('data-plot');
        syncPlotTabs();
        draw();
      });
    });

    $('in-wavelength').addEventListener('change', function () {
      state.instrument.wavelength = numberOr(this.value, 1.54);
    });
    $('in-i0').addEventListener('change', function () {
      state.instrument.i0 = numberOr(this.value, 1);
      syncParamValue('inst.i0', state.instrument.i0);
    });
    $('in-ibkg').addEventListener('change', function () {
      state.instrument.ibkg = numberOr(this.value, 0);
      syncParamValue('inst.ibkg', state.instrument.ibkg);
    });
    $('in-restype').addEventListener('change', function () {
      state.instrument.resolution.type = this.value;
    });
    $('in-res').addEventListener('change', function () {
      state.instrument.resolution.width = numberOr(this.value, 0);
    });
    $('in-respoints').addEventListener('change', function () {
      state.instrument.resolution.points = Math.max(3, Math.round(numberOr(this.value, 5)));
    });
    $('in-resspread').addEventListener('change', function () {
      state.instrument.resolution.spread = Math.max(1, numberOr(this.value, 2));
    });
    $('in-footype').addEventListener('change', function () {
      state.instrument.footprint.type = this.value;
    });
    $('in-beamw').addEventListener('change', function () {
      state.instrument.footprint.beam_width = numberOr(this.value, 0);
    });
    $('in-samplelen').addEventListener('change', function () {
      state.instrument.footprint.sample_length = numberOr(this.value, 0);
    });

    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'F9') {
        ev.preventDefault();
        simulate();
      } else if ((ev.ctrlKey || ev.metaKey) && (ev.key === 'f' || ev.key === 'F')) {
        ev.preventDefault();
        startFit();
      }
    });

    window.addEventListener('resize', function () { draw(); });
  }

  // ---------------------------------------------------------------- 启动
  bind();
  loadMaterials();
  syncParamsFromStack();
  renderDataList();
  renderStack();
  renderParams();
  syncPlotTabs();
  draw();
  if (location.search.indexOf('mock=1') >= 0) {
    var demoRows = [];
    for (var qi = 0; qi < 200; qi += 1) {
      var qv = 0.02 + qi * 0.0025;
      demoRows.push([qv, 1e-6 + 0.45 * Math.exp(-qv * 9) * (1 + 0.35 * Math.sin(qv * 60))]);
    }
    addDataset('示例数据', demoRows);
    simulate();
  }
  setStatus('就绪 · 导入数据后按 Ctrl+F 拟合');
})();
