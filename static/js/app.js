/* ============================================
   InstaBot — Frontend Application JavaScript
   ============================================ */

// ──────────────────────────────────────────────
// Toast Notification System
// ──────────────────────────────────────────────

const TOAST_ICONS = {
  success: '✅',
  error: '❌',
  warning: '⚠️',
  info: 'ℹ️',
};

function showToast(message, type = 'info', duration = 4000) {
  const container = document.getElementById('toastContainer');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <span class="toast-icon">${TOAST_ICONS[type] || TOAST_ICONS.info}</span>
    <span class="toast-message">${message}</span>
    <button class="toast-close" onclick="dismissToast(this)">✕</button>
  `;

  container.appendChild(toast);

  // Auto-remove after duration
  setTimeout(() => {
    dismissToast(toast.querySelector('.toast-close'));
  }, duration);
}

function dismissToast(btnOrEl) {
  const toast = btnOrEl.closest('.toast');
  if (!toast) return;
  toast.classList.add('removing');
  setTimeout(() => toast.remove(), 300);
}


// ──────────────────────────────────────────────
// API Helpers
// ──────────────────────────────────────────────

async function apiGet(url) {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    console.error('API GET Error:', err);
    showToast('Erro ao carregar dados.', 'error');
    throw err;
  }
}

async function apiPost(url, data, isFormData = false) {
  try {
    const options = {
      method: 'POST',
    };
    if (isFormData) {
      options.body = data;
    } else {
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify(data);
    }
    const res = await fetch(url, options);
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.error || `HTTP ${res.status}`);
    }
    return await res.json();
  } catch (err) {
    console.error('API POST Error:', err);
    showToast(err.message || 'Erro ao enviar dados.', 'error');
    throw err;
  }
}

async function apiDelete(url) {
  try {
    const res = await fetch(url, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    console.error('API DELETE Error:', err);
    showToast('Erro ao remover recurso.', 'error');
    throw err;
  }
}


// ──────────────────────────────────────────────
// Sidebar & Navigation
// ──────────────────────────────────────────────

function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebarOverlay');
  sidebar.classList.toggle('open');
  overlay.classList.toggle('active');
}


// ──────────────────────────────────────────────
// Connection Status
// ──────────────────────────────────────────────

async function checkConnection() {
  const dot = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  if (!dot || !text) return;

  try {
    const data = await apiGet('/api/status');
    if (data && data.profiles_logged_in > 0) {
      dot.className = 'status-dot online';
      text.textContent = `Instagram: ${data.profiles_logged_in} conta(s) conectada(s)`;
    } else if (data && data.profiles_count > 0) {
      dot.className = 'status-dot offline';
      text.textContent = `Instagram: ${data.profiles_count} conta(s), nenhuma logada`;
    } else {
      dot.className = 'status-dot offline';
      text.textContent = 'Instagram: Nenhuma conta';
    }
  } catch {
    dot.className = 'status-dot offline';
    text.textContent = 'Instagram: Offline';
  }
}


// ──────────────────────────────────────────────
// SSE (Server-Sent Events)
// ──────────────────────────────────────────────

function initSSE() {
  if (typeof EventSource === 'undefined') return;
  const es = new EventSource('/api/events');
  es.addEventListener('post_published', function(e) {
    const data = JSON.parse(e.data);
    showToast(`Post #${data.post_id} publicado com sucesso!`, 'success');
    if (typeof refreshDashboard === 'function') refreshDashboard();
  });
  es.addEventListener('post_failed', function(e) {
    const data = JSON.parse(e.data);
    showToast(`Post #${data.post_id} falhou: ${data.error}`, 'error', 8000);
    if (typeof refreshDashboard === 'function') refreshDashboard();
  });
  es.addEventListener('login_status', function(e) {
    checkConnection();
  });
}


// ──────────────────────────────────────────────
// Profile Loading
// ──────────────────────────────────────────────

async function loadProfiles() {
  const select = document.getElementById('profileSelect');
  if (!select) return;

  try {
    const data = await apiGet('/api/profiles');
    select.innerHTML = '<option value="" disabled selected>Selecionar perfil…</option>';

    if (data && data.profiles && data.profiles.length > 0) {
      data.profiles.forEach((profile) => {
        const opt = document.createElement('option');
        opt.value = profile.id;
        opt.textContent = profile.name || `Perfil ${profile.id}`;
        select.appendChild(opt);
      });
    } else {
      select.innerHTML = '<option value="" disabled selected>Nenhum perfil disponível</option>';
    }
  } catch {
    select.innerHTML = '<option value="" disabled selected>Erro ao carregar perfis</option>';
  }
}


// ──────────────────────────────────────────────
// Post Type Selection
// ──────────────────────────────────────────────

function selectPostType(card) {
  // Remove selected from all
  document.querySelectorAll('.type-card').forEach((c) => c.classList.remove('selected'));
  // Add to clicked
  card.classList.add('selected');

  const type = card.dataset.type;
  document.getElementById('postType').value = type;

  // Show/hide cover image for reels
  const coverGroup = document.getElementById('coverGroup');
  if (coverGroup) {
    coverGroup.classList.toggle('hidden', type !== 'reel');
  }

  // Update file input: single or multiple
  const mediaInput = document.getElementById('mediaInput');
  if (mediaInput) {
    if (type === 'carousel') {
      mediaInput.multiple = true;
      document.getElementById('uploadHint').textContent =
        'Selecione múltiplas imagens para o carrossel (2-10 imagens)';
    } else if (type === 'reel') {
      mediaInput.multiple = false;
      mediaInput.accept = 'video/*';
      document.getElementById('uploadHint').textContent =
        'Selecione um vídeo (MP4, MOV) — máx. 60 segundos';
    } else if (type === 'story') {
      mediaInput.multiple = false;
      document.getElementById('uploadHint').textContent =
        'Imagem (JPG, PNG) ou Vídeo (MP4, MOV) — máx. 15 segundos';
    } else {
      mediaInput.multiple = false;
      mediaInput.accept = 'image/*,video/*';
      document.getElementById('uploadHint').textContent =
        'Imagens (JPG, PNG, WebP) ou Vídeos (MP4, MOV) — máx. 50MB';
    }
  }
}


// ──────────────────────────────────────────────
// File Upload & Drag-and-Drop
// ──────────────────────────────────────────────

let selectedFiles = [];

function initUploadZone() {
  const zone = document.getElementById('uploadZone');
  const input = document.getElementById('mediaInput');
  if (!zone || !input) return;

  // Drag events
  zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    zone.classList.add('drag-over');
  });

  zone.addEventListener('dragleave', () => {
    zone.classList.remove('drag-over');
  });

  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    handleFiles(e.dataTransfer.files);
  });

  // File input change
  input.addEventListener('change', () => {
    handleFiles(input.files);
  });
}

function handleFiles(fileList) {
  if (!fileList || fileList.length === 0) return;

  const postType = document.getElementById('postType').value;

  if (postType !== 'carousel') {
    selectedFiles = [fileList[0]];
  } else {
    for (const f of fileList) {
      selectedFiles.push(f);
    }
    if (selectedFiles.length > 10) {
      showToast('Máximo de 10 imagens para carrossel.', 'warning');
      selectedFiles = selectedFiles.slice(0, 10);
    }
  }

  renderMediaPreviews();
  updatePreviewPanel();
}

function renderMediaPreviews() {
  const container = document.getElementById('mediaPreview');
  if (!container) return;

  container.innerHTML = '';

  selectedFiles.forEach((file, index) => {
    const item = document.createElement('div');
    item.className = 'media-preview-item';

    if (file.type.startsWith('image/')) {
      const img = document.createElement('img');
      img.src = URL.createObjectURL(file);
      img.alt = file.name;
      item.appendChild(img);
    } else if (file.type.startsWith('video/')) {
      const video = document.createElement('video');
      video.src = URL.createObjectURL(file);
      video.muted = true;
      item.appendChild(video);
    }

    const removeBtn = document.createElement('button');
    removeBtn.className = 'remove-media';
    removeBtn.textContent = '✕';
    removeBtn.onclick = () => removeFile(index);
    item.appendChild(removeBtn);

    container.appendChild(item);
  });
}

function removeFile(index) {
  selectedFiles.splice(index, 1);
  renderMediaPreviews();
  updatePreviewPanel();
}

function updatePreviewPanel() {
  const previewMedia = document.getElementById('previewMedia');
  if (!previewMedia) return;

  // Apply aspect ratio
  const ratioInput = document.getElementById('aspectRatio');
  const ratio = ratioInput ? ratioInput.value : 'original';
  const ratioMap = { '1:1': '1/1', '4:5': '4/5', '9:16': '9/16', '16:9': '16/9', 'original': '1' };
  previewMedia.style.aspectRatio = ratioMap[ratio] || '1';

  if (selectedFiles.length > 0) {
    const file = selectedFiles[0];
    if (file.type.startsWith('image/')) {
      previewMedia.innerHTML = `<img src="${URL.createObjectURL(file)}" alt="Preview" style="width:100%;height:100%;object-fit:cover;">`;
    } else if (file.type.startsWith('video/')) {
      previewMedia.innerHTML = `<video src="${URL.createObjectURL(file)}" muted autoplay loop style="width:100%;height:100%;object-fit:cover;"></video>`;
    }
  } else {
    previewMedia.innerHTML = '📷';
  }
}


// ──────────────────────────────────────────────
// Aspect Ratio Selection
// ──────────────────────────────────────────────

function selectRatio(card) {
  document.querySelectorAll('#ratioSelector .type-card').forEach((c) => c.classList.remove('selected'));
  card.classList.add('selected');
  document.getElementById('aspectRatio').value = card.dataset.ratio;
  updatePreviewPanel();
}


// ──────────────────────────────────────────────
// AI Caption Generation
// ──────────────────────────────────────────────

async function generateCaption() {
  if (selectedFiles.length === 0) {
    showToast('Adicione uma mídia primeiro.', 'warning');
    return;
  }

  const btn = document.getElementById('generateCaptionBtn');
  const textarea = document.getElementById('captionInput');
  const toneSelect = document.getElementById('aiTone');
  if (!btn || !textarea) return;

  const tone = toneSelect ? toneSelect.value : 'descontraido';

  btn.disabled = true;
  btn.textContent = '⏳ Gerando...';

  try {
    const formData = new FormData();
    formData.append('media', selectedFiles[0]);
    formData.append('tone', tone);

    const resp = await fetch('/api/ai/caption', { method: 'POST', body: formData });
    const data = await resp.json();

    if (data.success && data.caption) {
      textarea.value = data.caption;
      textarea.dispatchEvent(new Event('input'));
      showToast('Copy gerado com sucesso!', 'success');
    } else {
      showToast(data.error || 'Erro ao gerar copy.', 'error');
    }
  } catch (e) {
    showToast('Erro de conexão com a IA.', 'error');
  }

  btn.disabled = false;
  btn.textContent = '✨ Gerar Copy com IA';
}


// ──────────────────────────────────────────────
// Caption Character Counter
// ──────────────────────────────────────────────

function initCharCounter() {
  const textarea = document.getElementById('captionInput');
  const counter = document.getElementById('charCount');
  const counterWrap = document.getElementById('charCounter');
  if (!textarea || !counter) return;

  textarea.addEventListener('input', () => {
    const len = textarea.value.length;
    counter.textContent = len.toLocaleString('pt-BR');

    if (counterWrap) {
      counterWrap.classList.remove('warning', 'danger');
      if (len > 2000) {
        counterWrap.classList.add('danger');
      } else if (len > 1800) {
        counterWrap.classList.add('warning');
      }
    }

    // Live preview caption
    const previewCaption = document.getElementById('previewCaption');
    if (previewCaption) {
      if (textarea.value.trim()) {
        previewCaption.textContent = textarea.value;
      } else {
        previewCaption.innerHTML =
          '<span class="text-muted" style="font-style: italic;">A legenda aparecerá aqui…</span>';
      }
    }
  });
}


// ──────────────────────────────────────────────
// Publish Now Toggle
// ──────────────────────────────────────────────

function initPublishToggle() {
  const toggle = document.getElementById('publishNowToggle');
  const timeGroup = document.getElementById('scheduleTimeGroup');
  const submitText = document.getElementById('submitBtnText');
  const timeInput = document.getElementById('scheduledTime');

  if (!toggle || !timeGroup) return;

  toggle.addEventListener('change', () => {
    if (toggle.checked) {
      timeGroup.style.display = 'none';
      if (timeInput) timeInput.removeAttribute('required');
      if (submitText) submitText.textContent = '🚀 Publicar Agora';
    } else {
      timeGroup.style.display = 'block';
      if (timeInput) timeInput.setAttribute('required', '');
      if (submitText) submitText.textContent = '📅 Agendar Post';
    }
  });
}


// ──────────────────────────────────────────────
// Schedule Form Submission
// ──────────────────────────────────────────────

function initScheduleForm() {
  initUploadZone();
  initCharCounter();
  initPublishToggle();
  loadProfiles();

  // Set minimum datetime to now
  const timeInput = document.getElementById('scheduledTime');
  if (timeInput) {
    const now = new Date();
    now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
    timeInput.min = now.toISOString().slice(0, 16);
  }

  const form = document.getElementById('scheduleForm');
  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();

    // Validate post type
    const postType = document.getElementById('postType').value;
    if (!postType) {
      showToast('Selecione o tipo de post.', 'warning');
      return;
    }

    // Validate media
    if (selectedFiles.length === 0) {
      showToast('Adicione pelo menos um arquivo de mídia.', 'warning');
      return;
    }

    // Validate datetime
    const publishNow = document.getElementById('publishNowToggle').checked;
    const scheduledTime = document.getElementById('scheduledTime').value;

    if (!publishNow && !scheduledTime) {
      showToast('Selecione a data e hora de agendamento.', 'warning');
      return;
    }

    if (!publishNow && scheduledTime) {
      const scheduled = new Date(scheduledTime);
      if (scheduled <= new Date()) {
        showToast('A data de agendamento deve ser no futuro.', 'warning');
        return;
      }
    }

    // Build FormData
    const formData = new FormData(form);

    // Replace file input with selectedFiles
    formData.delete('media');
    selectedFiles.forEach((file) => {
      formData.append('media', file);
    });

    // Loading state
    const btn = document.getElementById('submitBtn');
    btn.classList.add('loading');

    try {
      const result = await apiPost('/api/posts', formData, true);
      showToast('Post agendado com sucesso! 🎉', 'success');

      // Reset form
      form.reset();
      selectedFiles = [];
      renderMediaPreviews();
      updatePreviewPanel();
      document.querySelectorAll('.type-card').forEach((c) => c.classList.remove('selected'));
      document.getElementById('postType').value = '';
      const previewCaption = document.getElementById('previewCaption');
      if (previewCaption) {
        previewCaption.innerHTML =
          '<span class="text-muted" style="font-style: italic;">A legenda aparecerá aqui…</span>';
      }

      // Redirect after short delay
      setTimeout(() => {
        window.location.href = '/posts';
      }, 1500);
    } catch (err) {
      // Error toast already shown by apiPost
    } finally {
      btn.classList.remove('loading');
    }
  });
}


// ──────────────────────────────────────────────
// Posts Page — Filter Functionality
// ──────────────────────────────────────────────

function filterPosts(status, tabBtn) {
  // Update active tab
  document.querySelectorAll('.filter-tab').forEach((t) => t.classList.remove('active'));
  tabBtn.classList.add('active');

  // Filter cards
  const cards = document.querySelectorAll('.post-card');
  cards.forEach((card) => {
    if (status === 'all') {
      card.style.display = '';
    } else {
      card.style.display = card.dataset.status === status ? '' : 'none';
    }
  });

  // Check if any visible
  const visibleCards = document.querySelectorAll('.post-card[style=""], .post-card:not([style])');
  // Handle empty state if needed
}

function initPostsPage() {
  // Could do additional initialization here
}


// ──────────────────────────────────────────────
// Post Actions
// ──────────────────────────────────────────────

function viewPost(postId) {
  window.location.href = `/posts/${postId}`;
}

async function publishNow(postId) {
  showConfirm(
    'Publicar Agora',
    'Deseja publicar este post imediatamente?',
    async () => {
      try {
        await apiPost(`/api/posts/${postId}/publish`);
        showToast('Post enviado para publicação! 🚀', 'success');
        setTimeout(() => location.reload(), 1500);
      } catch {
        // Error shown by apiPost
      }
    },
    'btn-success',
    'Publicar'
  );
}

function confirmDeletePost(postId) {
  showConfirm(
    'Cancelar Post',
    'Tem certeza que deseja cancelar e remover este post? Esta ação não pode ser desfeita.',
    async () => {
      try {
        await apiDelete(`/api/posts/${postId}`);
        showToast('Post removido com sucesso.', 'success');

        // Animate removal
        const card = document.querySelector(`.post-card[data-id="${postId}"]`);
        if (card) {
          card.style.transition = 'all 0.3s ease';
          card.style.opacity = '0';
          card.style.transform = 'scale(0.95)';
          setTimeout(() => card.remove(), 300);
        }

        // Also remove table row if on dashboard
        setTimeout(() => location.reload(), 1000);
      } catch {
        // Error shown by apiDelete
      }
    }
  );
}


// ──────────────────────────────────────────────
// Confirm Modal
// ──────────────────────────────────────────────

function showConfirm(title, message, onConfirm, btnClass = 'btn-danger', btnText = 'Confirmar') {
  const modal = document.getElementById('confirmModal');
  const titleEl = document.getElementById('confirmTitle');
  const msgEl = document.getElementById('confirmMessage');
  const actionBtn = document.getElementById('confirmAction');

  if (!modal) return;

  titleEl.textContent = title;
  msgEl.textContent = message;

  // Reset button classes
  actionBtn.className = `btn ${btnClass}`;
  actionBtn.textContent = btnText;

  // Clone to remove old listeners
  const newBtn = actionBtn.cloneNode(true);
  actionBtn.parentNode.replaceChild(newBtn, actionBtn);
  newBtn.id = 'confirmAction';

  newBtn.addEventListener('click', () => {
    closeModal('confirmModal');
    if (onConfirm) onConfirm();
  });

  openModal('confirmModal');
}

function openModal(id) {
  const modal = document.getElementById(id);
  if (modal) {
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
  }
}

function closeModal(id) {
  const modal = document.getElementById(id);
  if (modal) {
    modal.classList.remove('active');
    document.body.style.overflow = '';
  }
}

// Close modal on overlay click
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal-overlay') && e.target.classList.contains('active')) {
    e.target.classList.remove('active');
    document.body.style.overflow = '';
  }
});

// Close modal on Escape
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay.active').forEach((m) => {
      m.classList.remove('active');
    });
    document.body.style.overflow = '';
  }
});


// ──────────────────────────────────────────────
// Dashboard Auto-Refresh
// ──────────────────────────────────────────────

async function refreshDashboard() {
  try {
    const data = await apiGet('/api/dashboard');
    if (!data) return;

    // Update stat values
    if (data.stats) {
      const statCards = document.querySelectorAll('.stat-card');
      const values = [data.stats.total, data.stats.published, data.stats.pending, data.stats.failed];
      statCards.forEach((card, i) => {
        const valEl = card.querySelector('.stat-value');
        if (valEl && values[i] !== undefined) {
          const newVal = values[i];
          const oldVal = parseInt(valEl.textContent) || 0;
          if (newVal !== oldVal) {
            valEl.textContent = newVal;
            // Subtle animation
            valEl.style.transform = 'scale(1.1)';
            setTimeout(() => (valEl.style.transform = ''), 200);
          }
        }
      });
    }
  } catch {
    // Silently fail on auto-refresh
  }
}


// ──────────────────────────────────────────────
// Media Folder Browser
// ──────────────────────────────────────────────

async function loadMediaFiles() {
  const browser = document.getElementById('mediaBrowser');
  if (!browser) return;

  try {
    const data = await apiGet('/api/media');
    if (!data || !data.files || data.files.length === 0) {
      browser.innerHTML = `
        <div style="padding: 24px; text-align: center; color: var(--text-muted); font-size: 0.85rem;">
          📂 Nenhum arquivo encontrado
        </div>`;
      return;
    }

    browser.innerHTML = '';
    data.files.forEach((file) => {
      const item = document.createElement('div');
      item.className = 'media-browser-item';
      item.onclick = () => selectMediaFile(file);

      const isVideo = /\.(mp4|mov|avi|webm)$/i.test(file.name);
      const icon = isVideo ? '🎬' : '🖼️';

      item.innerHTML = `
        <span class="file-icon">${icon}</span>
        <span class="file-name">${file.name}</span>
        <span class="file-size">${formatFileSize(file.size)}</span>
      `;
      browser.appendChild(item);
    });
  } catch {
    browser.innerHTML = `
      <div style="padding: 24px; text-align: center; color: var(--text-muted); font-size: 0.85rem;">
        ⚠️ Erro ao carregar arquivos
      </div>`;
  }
}

function selectMediaFile(file) {
  showToast(`Arquivo selecionado: ${file.name}`, 'info');
  // Could integrate with upload: fetch and add to selectedFiles
}

function formatFileSize(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) {
    bytes /= 1024;
    i++;
  }
  return `${bytes.toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}


// ──────────────────────────────────────────────
// Button Loading States
// ──────────────────────────────────────────────

function setLoading(btn, loading) {
  if (!btn) return;
  if (loading) {
    btn.classList.add('loading');
    btn.disabled = true;
  } else {
    btn.classList.remove('loading');
    btn.disabled = false;
  }
}


// ──────────────────────────────────────────────
// Initialization
// ──────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  checkConnection();
  initSSE();
  setInterval(checkConnection, 60000);

  if (document.getElementById('profileSelect')) {
    loadProfiles();
  }
});
