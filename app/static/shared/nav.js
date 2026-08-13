
function openSearchModal() {
    document.getElementById('searchModal').style.display = 'block';
}

function closeSearchModal() {
    document.getElementById('searchModal').style.display = 'none';
}

function performSearch() {
    const query = document.getElementById('searchInput').value;
    const videoType = document.querySelector('input[name="videoType"]:checked').value;
    
    let searchParams = new URLSearchParams();
    searchParams.append('q', query);
    searchParams.append('type', videoType);
    
    window.location.href = `${SCRIPT_ROOT}/search?${searchParams.toString()}`;
}

// 点击模态框外部关闭
window.onclick = function(event) {
    if (event.target == document.getElementById('searchModal')) {
        closeSearchModal();
    }
    // 点击通知面板外部关闭
    const wrapper = document.getElementById('notifWrapper');
    if (wrapper && !wrapper.contains(event.target)) {
        closeNotifPanel();
    }
}

/* ── 通知系统 JS ── */
let notifPanelOpen = false;

function toggleNotifPanel() {
    notifPanelOpen ? closeNotifPanel() : openNotifPanel();
}

function openNotifPanel() {
    const panel = document.getElementById('notifPanel');
    const btn   = document.querySelector('.notif-bell-btn');
    if (btn) {
        const rect = btn.getBoundingClientRect();
        panel.style.top = (rect.bottom + 8) + 'px';
    }
    panel.style.display = 'block';
    notifPanelOpen = true;
    loadNotifications();
}

function closeNotifPanel() {
    document.getElementById('notifPanel').style.display = 'none';
    notifPanelOpen = false;
}

function loadNotifications() {
    fetch(SCRIPT_ROOT + '/api/notifications?per_page=10')
        .then(r => r.json())
        .then(data => {
            const list = document.getElementById('notifList');
            if (!data.notifications || data.notifications.length === 0) {
                list.innerHTML = '<div class="notif-empty">暂无通知</div>';
                return;
            }
            list.innerHTML = data.notifications.map(n => `
                <a class="notif-item ${n.is_read ? '' : 'unread'}"
                   href="${n.link ? (SCRIPT_ROOT + n.link) : '#'}"
                   onclick="markRead(${n.id}, this, event)">
                    <span class="notif-item-title">${n.title}</span>
                    <span class="notif-item-content">${n.content}</span>
                    <span class="notif-item-time">${n.created_at}</span>
                </a>
            `).join('');
        })
        .catch(() => {
            document.getElementById('notifList').innerHTML =
                '<div class="notif-empty">加载失败，请重试</div>';
        });
}

function markRead(id, el, event) {
    fetch(`${SCRIPT_ROOT}/api/notifications/${id}/read`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success && el) {
                el.classList.remove('unread');
                refreshBadge();
            }
        });
    // 如果有跳转链接则正常跳转，无链接则阻止默认行为
    const href = el.getAttribute('href');
    if (!href || href === '#') event.preventDefault();
}

function markAllRead() {
    fetch(SCRIPT_ROOT + '/api/notifications/read_all', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                document.querySelectorAll('.notif-item.unread')
                    .forEach(el => el.classList.remove('unread'));
                refreshBadge();
            }
        });
}

function refreshBadge() {
    fetch(SCRIPT_ROOT + '/api/notifications/unread_count')
        .then(r => r.json())
        .then(data => {
            const badge = document.getElementById('notifBadge');
            if (!badge) return;
            const count = data.unread_count || 0;
            badge.textContent = count > 99 ? '99+' : count;
            badge.style.display = count > 0 ? 'inline-block' : 'none';
        });
}

// 页面加载后立即拉取未读数，之后每 60 秒轮询一次
document.addEventListener('DOMContentLoaded', () => {
    // 仅登录状态下执行
    if (document.getElementById('notifBadge')) {
        refreshBadge();
        setInterval(refreshBadge, 60000);
    }
});
