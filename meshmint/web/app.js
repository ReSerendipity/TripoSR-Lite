import * as THREE from 'three';
import { OrbitControls } from 'three/addons/OrbitControls.js';

const el = (id) => document.getElementById(id);

const ui = {
  statusPill: el('statusPill'),
  verificationPill: el('verificationPill'),
  dropZone: el('dropZone'),
  fileInput: el('fileInput'),
  fileHint: el('fileHint'),
  imageRow: el('imageRow'),
  originalPreview: el('originalPreview'),
  maskPreview: el('maskPreview'),
  ratioBox: el('ratioBox'),
  ratioValue: el('ratioValue'),
  ratioBar: el('ratioBar'),
  ratioAdvice: el('ratioAdvice'),
  backgroundHint: el('backgroundHint'),
  stageLabel: el('stageLabel'),
  progressPct: el('progressPct'),
  progressBar: el('progressBar'),
  stageList: el('stageList'),
  generateBtn: el('generateBtn'),
  cancelBtn: el('cancelBtn'),
  jobNote: el('jobNote'),
  modelCard: el('modelCard'),
  modelState: el('modelState'),
  modelReasons: el('modelReasons'),
  downloadCmd: el('downloadCmd'),
  copyCmd: el('copyCmd'),
  metaCode: el('metaCode'),
  metaWeights: el('metaWeights'),
  metaLicense: el('metaLicense'),
  metaSize: el('metaSize'),
  metaDir: el('metaDir'),
  metaVerification: el('metaVerification'),
  demoBadge: el('demoBadge'),
  meshMeta: el('meshMeta'),
  viewport: el('viewport'),
  viewerEmpty: el('viewerEmpty'),
  meshStats: el('meshStats'),
  resetViewBtn: el('resetViewBtn'),
  wireframeBtn: el('wireframeBtn'),
  exportObjBtn: el('exportObjBtn'),
  exportGlbBtn: el('exportGlbBtn'),
  exportNote: el('exportNote'),
  toast: el('toast'),
};

const state = {
  file: null,
  job: null,
  mesh: null,
  polling: false,
  objectUrl: null,
  modelCard: null,
};

/* ---------------------------------------------------------------- helpers */
function toast(message, isError = false) {
  ui.toast.textContent = message;
  ui.toast.classList.toggle('err', Boolean(isError));
  ui.toast.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => ui.toast.classList.remove('show'), 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = payload?.error?.message || `请求失败（HTTP ${response.status}）`;
    const error = new Error(message);
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  return payload;
}

function setPill(text, kind = 'plain') {
  ui.statusPill.textContent = text;
  ui.statusPill.className = `pill ${kind === 'plain' ? 'pill--plain' : `pill--${kind}`}`;
}

function jobStatusVisual(job) {
  const map = {
    QUEUED: ['排队中', 'busy'],
    RUNNING: ['推理中', 'busy'],
    NOT_READY: ['模型未就绪', 'warn'],
    DONE: ['完成', 'ok'],
    FAILED: ['失败', 'err'],
    TIMEOUT: ['超时', 'err'],
    CANCEL_REQUESTED: ['取消中', 'busy'],
    CANCELLED: ['已取消', 'warn'],
  };
  return map[job.status] || [job.status, 'plain'];
}

function fmtInt(value) {
  return Number(value || 0).toLocaleString('zh-CN');
}

/* ---------------------------------------------------------------- three.js */
const view = {
  renderer: null,
  scene: null,
  camera: null,
  controls: null,
  group: null,
  object: null,
  material: null,
  fit: null,
  ready: false,
};

function initViewer() {
  const viewport = ui.viewport;
  const width = Math.max(viewport.clientWidth, 320);
  const height = Math.max(viewport.clientHeight, 320);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(width, height);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.12;
  renderer.domElement.setAttribute('aria-label', '3D 网格预览');
  viewport.insertBefore(renderer.domElement, viewport.firstChild);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0c1116);
  scene.fog = new THREE.Fog(0x0c1116, 4.2, 9);

  const camera = new THREE.PerspectiveCamera(42, width / height, 0.01, 100);
  camera.position.set(1.5, 1.15, 1.95);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.075;
  controls.minDistance = 0.35;
  controls.maxDistance = 9;
  controls.target.set(0, 0, 0);

  scene.add(new THREE.HemisphereLight(0xa9d8ff, 0x211709, 0.55));
  const key = new THREE.DirectionalLight(0xffffff, 2.4);
  key.position.set(2.4, 3.2, 2.2);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffcf9a, 0.85);
  fill.position.set(-2.6, 0.9, -1.8);
  scene.add(fill);

  const grid = new THREE.GridHelper(3.2, 16, 0x2c4050, 0x18242e);
  grid.position.y = -0.62;
  scene.add(grid);

  const group = new THREE.Group();
  scene.add(group);

  Object.assign(view, { renderer, scene, camera, controls, group, ready: true });

  const animate = () => {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  };
  animate();

  const resize = () => {
    const w = Math.max(viewport.clientWidth, 320);
    const h = Math.max(viewport.clientHeight, 320);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  };
  new ResizeObserver(resize).observe(viewport);
}

function disposeObject() {
  if (!view.object) return;
  view.group.remove(view.object);
  view.object.geometry?.dispose();
  view.object.material?.dispose();
  view.object = null;
  view.material = null;
}

function fitView(geometry) {
  geometry.computeBoundingBox();
  const box = geometry.boundingBox;
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z, 0.2) * 0.72;
  const distance = radius * 3.1;

  view.controls.target.copy(center);
  view.camera.near = Math.max(0.001, distance / 100);
  view.camera.far = distance * 20;
  view.camera.updateProjectionMatrix();

  const dir = new THREE.Vector3(0.62, 0.48, 0.78).normalize();
  view.camera.position.copy(center).add(dir.multiplyScalar(distance));
  view.controls.update();
  view.fit = { center: center.clone(), distance };
}

function renderMesh(meshPayload) {
  if (!view.ready) return;
  disposeObject();

  const positions = meshPayload.positions || [];
  const indices = meshPayload.indices || [];
  const colors = meshPayload.colors || [];
  if (positions.length < 9 || indices.length < 3) {
    toast('网格数据为空，无法预览。', true);
    return;
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(positions), 3));
  geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(indices), 1));
  if (colors.length === positions.length) {
    const linear = new Float32Array(colors.length);
    for (let i = 0; i < colors.length; i += 1) {
      const c = colors[i];
      linear[i] = c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    }
    geometry.setAttribute('color', new THREE.BufferAttribute(linear, 3));
  }
  geometry.computeVertexNormals();
  geometry.computeBoundingBox();

  const material = new THREE.MeshStandardMaterial({
    color: colors.length === positions.length ? 0xffffff : 0xb9c8d4,
    vertexColors: colors.length === positions.length,
    roughness: 0.58,
    metalness: 0.12,
    flatShading: false,
  });

  const object = new THREE.Mesh(geometry, material);
  view.group.add(object);
  view.object = object;
  view.material = material;
  fitView(geometry);

  ui.viewerEmpty.hidden = true;
  state.mesh = meshPayload;

  const stats = meshPayload.stats || {};
  ui.demoBadge.hidden = !meshPayload.demo;
  ui.meshMeta.textContent = meshPayload.demo ? 'DEMO placeholder' : 'TripoSR output';
  ui.meshStats.textContent = `${fmtInt(stats.vertices)} verts · ${fmtInt(stats.faces)} faces · watertight ${stats.watertight ? 'yes' : 'no'}`;
  ui.exportNote.textContent = meshPayload.demo
    ? '当前为 DEMO 演示网格，导出文件会带 DEMO 标记'
    : '导出为本地文件，不上传任何内容';
}

function resetView() {
  if (!view.fit) return;
  const { center, distance } = view.fit;
  const dir = new THREE.Vector3(0.62, 0.48, 0.78).normalize();
  view.controls.target.copy(center);
  view.camera.position.copy(center).add(dir.multiplyScalar(distance));
  view.controls.update();
}

/* ---------------------------------------------------------------- model card */
function renderModelCard(probe, model) {
  state.modelCard = { probe, model };
  if (!probe) return;

  ui.modelState.textContent = probe.state;
  ui.modelState.style.color = probe.state === 'READY' ? 'var(--accent)' : 'var(--amber)';
  ui.modelReasons.replaceChildren();

  const reasons = [...(probe.reasons || []), ...(probe.warnings || [])];
  for (const reason of reasons) {
    const node = document.createElement('div');
    node.className = 'reason';
    const code = document.createElement('code');
    code.textContent = reason.code;
    node.append(code, document.createElement('br'), document.createTextNode(reason.message || ''));
    if (reason.hint) {
      const hint = document.createElement('div');
      hint.style.color = 'var(--muted)';
      hint.style.marginTop = '3px';
      hint.textContent = reason.hint;
      node.append(hint);
    }
    ui.modelReasons.append(node);
  }
  if (!reasons.length) {
    const node = document.createElement('div');
    node.className = 'reason';
    node.style.borderLeftColor = 'var(--accent)';
    node.textContent = '全部依赖就绪，可以执行真实推理。';
    ui.modelReasons.append(node);
  }

  const details = probe.details || {};
  if (model) {
    ui.downloadCmd.textContent = model.download_command || '—';
    ui.metaCode.textContent = `${model.code_url}（${model.code_license}）`;
    ui.metaWeights.textContent = `${model.upstream_weights_url}（${model.weights_license}，不入库）`;
    const aux = (model.auxiliary_weights || []).map((item) => `${item.id} ${item.license}`).join('；');
    ui.metaLicense.textContent = `代码 ${model.code_license} / 权重 ${model.weights_license}${aux ? `；附带 ${aux}` : ''}`;
    ui.metaSize.textContent = `${model.weight_files?.[0]?.expected_label || '—'}；官方口径 ${model.official_hardware_claim?.vram_gb || '?'}GB 显存`;
    ui.metaVerification.textContent = model.verification_status || probe.state;
    ui.verificationPill.textContent = model.verification_status || 'CODE_READY_MODEL_UNTESTED';
  }
  ui.metaDir.textContent = details.weights_dir || '—';

  if (probe.state === 'READY') {
    setPill('模型就绪', 'ok');
  } else {
    setPill('模型未就绪', 'warn');
  }
}

/* ---------------------------------------------------------------- progress */
function updateProgress(job) {
  const percent = Math.round((job.progress || 0) * 100);
  ui.progressBar.style.width = `${percent}%`;
  ui.progressPct.textContent = `${percent}%`;
  ui.stageLabel.textContent = job.stage_label || '等待任务';

  const order = ['load_model', 'preprocess', 'forward', 'extract'];
  const currentIndex = order.indexOf(job.stage);
  for (const chip of ui.stageList.children) {
    const index = order.indexOf(chip.dataset.stage);
    chip.classList.toggle('done', job.stage === 'done' || (currentIndex >= 0 && index < currentIndex));
    chip.classList.toggle('active', index === currentIndex && job.stage !== 'done');
  }
  if (job.status === 'DONE') {
    for (const chip of ui.stageList.children) {
      chip.classList.add('done');
      chip.classList.remove('active');
    }
  }
}

function applyJob(payload, { includeProbe = false } = {}) {
  const job = payload.job;
  state.job = job;
  updateProgress(job);

  const [text, kind] = jobStatusVisual(job);
  setPill(text, kind);
  ui.jobNote.textContent = job.message || '';

  ui.cancelBtn.disabled = !['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(job.status);
  const busy = ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(job.status);
  ui.generateBtn.disabled = busy || !state.file;

  if (payload.mesh) {
    renderMesh(payload.mesh);
  }

  const hasMesh = Boolean(job.mesh_id);
  ui.exportObjBtn.disabled = !hasMesh;
  ui.exportGlbBtn.disabled = !hasMesh;
  if (hasMesh) {
    ui.exportNote.textContent = job.demo
      ? '当前为 DEMO 演示网格，导出文件会带 DEMO 标记'
      : '导出为本地文件，不上传任何内容';
  }

  if (includeProbe && payload.probe) {
    renderModelCard(payload.probe, state.modelCard?.model || null);
  }

  if (job.status === 'FAILED' || job.status === 'TIMEOUT') {
    toast(job.message || '任务失败', true);
  }
  return job;
}

async function pollJob(jobId) {
  if (state.polling) return;
  state.polling = true;
  try {
    for (let attempt = 0; attempt < 400; attempt += 1) {
      const payload = await api(`/api/jobs/${jobId}`);
      const job = applyJob(payload);
      if (job.terminal) break;
      await new Promise((resolve) => setTimeout(resolve, 550));
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.polling = false;
  }
}

/* ---------------------------------------------------------------- file flow */
function setPreview(file) {
  if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
  state.objectUrl = URL.createObjectURL(file);
  ui.originalPreview.src = state.objectUrl;
  ui.imageRow.hidden = false;
}

async function inspectFile(file) {
  const form = new FormData();
  form.append('file', file, file.name || 'upload.png');
  const payload = await api('/api/inspect', { method: 'POST', body: form });
  const image = payload.image;
  const hint = payload.foreground || {};

  ui.maskPreview.src = hint.mask_data_url || '';
  ui.ratioBox.hidden = !hint.mask_data_url;
  const ratio = Number(hint.foreground_ratio || 0);
  ui.ratioValue.textContent = `${(ratio * 100).toFixed(1)}%`;
  ui.ratioBar.style.width = `${Math.min(ratio * 100, 100)}%`;
  ui.ratioAdvice.textContent = [hint.advice, hint.note, ...(image.warnings || [])]
    .filter(Boolean)
    .join(' ');
  ui.backgroundHint.textContent = hint.background_hint || '';
  ui.fileHint.textContent = `${image.format} · ${image.width}×${image.height} · ${(image.size_bytes / 1024).toFixed(0)} KB`;
}

async function handleFile(file) {
  if (!file) return;
  if (!/image\/(png|jpeg)/.test(file.type) && !/\.(png|jpg|jpeg)$/i.test(file.name)) {
    toast('只支持 PNG / JPEG。', true);
    return;
  }
  state.file = file;
  setPreview(file);
  ui.generateBtn.disabled = false;
  ui.jobNote.textContent = '图片已载入，等待生成。';
  try {
    await inspectFile(file);
  } catch (error) {
    ui.generateBtn.disabled = true;
    ui.ratioBox.hidden = true;
    ui.jobNote.textContent = error.message;
    toast(error.message, true);
  }
}

async function submitJob() {
  if (!state.file) return;
  const form = new FormData();
  form.append('file', state.file, state.file.name || 'upload.png');
  ui.generateBtn.disabled = true;
  setPill('提交中…', 'busy');
  try {
    const payload = await api('/api/jobs', { method: 'POST', body: form });
    const job = applyJob(payload, { includeProbe: true });
    if (!job.terminal) pollJob(job.id);
  } catch (error) {
    toast(error.message, true);
    ui.jobNote.textContent = error.message;
    ui.generateBtn.disabled = false;
    setPill('提交失败', 'err');
  }
}

async function cancelJob() {
  if (!state.job) return;
  try {
    const payload = await api(`/api/jobs/${state.job.id}/cancel`, { method: 'POST' });
    applyJob(payload);
    toast('已请求取消，取消在阶段边界生效。');
  } catch (error) {
    toast(error.message, true);
  }
}

function exportMesh(format) {
  if (!state.job || !state.job.mesh_id) {
    toast('当前没有可导出的网格。', true);
    return;
  }
  window.location.href = `/api/jobs/${state.job.id}/export?format=${format}`;
}

/* ---------------------------------------------------------------- startup */
async function boot() {
  initViewer();

  try {
    const modelPayload = await api('/api/model');
    renderModelCard(modelPayload.probe, modelPayload.model);
  } catch (error) {
    setPill('后端不可达', 'err');
    toast(error.message, true);
  }

  try {
    const demo = await api('/api/demo');
    renderMesh(demo.mesh);
  } catch (error) {
    ui.viewerEmpty.textContent = '演示网格加载失败';
    toast(error.message, true);
  }
}

ui.fileInput.addEventListener('change', (event) => {
  const file = event.target.files?.[0];
  handleFile(file);
});

for (const eventName of ['dragenter', 'dragover']) {
  ui.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    ui.dropZone.classList.add('dragover');
  });
}
for (const eventName of ['dragleave', 'drop']) {
  ui.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    ui.dropZone.classList.remove('dragover');
  });
}
ui.dropZone.addEventListener('drop', (event) => {
  const file = event.dataTransfer?.files?.[0];
  handleFile(file);
});

ui.generateBtn.addEventListener('click', submitJob);
ui.cancelBtn.addEventListener('click', cancelJob);
ui.exportObjBtn.addEventListener('click', () => exportMesh('obj'));
ui.exportGlbBtn.addEventListener('click', () => exportMesh('glb'));
ui.resetViewBtn.addEventListener('click', resetView);
ui.wireframeBtn.addEventListener('click', () => {
  if (!view.material) return;
  view.material.wireframe = !view.material.wireframe;
  ui.wireframeBtn.textContent = view.material.wireframe ? '实体' : '线框';
});

ui.copyCmd.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(ui.downloadCmd.textContent || '');
    toast('下载命令已复制。');
  } catch (error) {
    toast('复制失败，请手动选中命令。', true);
  }
});

boot();
