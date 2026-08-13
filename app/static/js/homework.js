// 模态框控制
function openUploadModal() {
    const modal = document.getElementById('upload-modal');
    if (modal) {
        modal.style.display = 'block';
    }
}

function closeUploadModal() {
    const modal = document.getElementById('upload-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}

// 视频上传处理
document.addEventListener('DOMContentLoaded', function() {
    const uploadForm = document.getElementById('upload-form');
    if (uploadForm) {
        uploadForm.addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const formData = new FormData(this);
            
            try {
                const response = await fetch(SCRIPT_ROOT + '/api/videos/upload', {
                    method: 'POST',
                    body: formData
                });
                
                const result = await response.json();
                
                if (response.ok) {
                    alert('视频上传成功！');
                    closeUploadModal();
                    // 刷新页面显示新视频
                    window.location.reload();
                } else {
                    alert(`上传失败：${result.message}`);
                }
            } catch (error) {
                console.error('Upload error:', error);
                alert('上传出错，请稍后重试');
            }
        });
    }
});

// 评分功能
async function rateVideo(videoId) {
    const ratingInput = document.getElementById(`rating-${videoId}`);
    const rating = parseInt(ratingInput.value);
    
    if (isNaN(rating) || rating < 1 || rating > 10) {
        alert('请输入1-10之间的评分');
        return;
    }

    try {
        const response = await fetch(SCRIPT_ROOT + `/api/videos/${videoId}/rate`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ rating })
        });
        
        const data = await response.json();
        if (response.ok) {
            const rateBtn = ratingInput.nextElementSibling;
            rateBtn.textContent = '已评分';
            rateBtn.classList.add('rated');
            updateVideoStats(videoId, data);
        }
    } catch (error) {
        console.error('Rating error:', error);
    }
}

// 收藏功能
async function toggleFavorite(videoId) {
    try {
        const response = await fetch(SCRIPT_ROOT + `/api/videos/${videoId}/favorite`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        });
        
        const data = await response.json();
        if (response.ok) {
            const favoriteBtn = document.querySelector(`#video-${videoId} .favorite-btn`);
            favoriteBtn.textContent = data.is_favorited ? '已收藏' : '收藏';
            favoriteBtn.classList.toggle('favorited', data.is_favorited);
            updateVideoStats(videoId, data);
        }
    } catch (error) {
        console.error('Favorite error:', error);
    }
}

// 添加评论（空内容时触发语音输入）
async function addComment(videoId) {
    const input = document.getElementById(`comment-input-${videoId}`);
    const content = input.value.trim();

    if (!content) {
        if (typeof openVoiceCommentModal === 'function') {
            openVoiceCommentModal(videoId);
        }
        return;
    }
    await _submitComment(videoId, content, input);
}

// 内部：实际提交评论
async function _submitComment(videoId, content, inputEl) {
    try {
        const response = await fetch(`${SCRIPT_ROOT}/api/videos/${videoId}/comments`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content })
        });
        const data = await response.json();
        if (response.ok) {
            if (inputEl) inputEl.value = '';
            const commentsList = document.getElementById(`comments-${videoId}`);
            if (commentsList) {
                const div = document.createElement('div');
                div.className = 'comment-item';
                if (data.comment.id) div.dataset.commentId = data.comment.id;
                div.innerHTML = `
                    <div class="comment-header">
                        <span class="comment-author">${data.comment.author}</span>
                        <span class="comment-time">${data.comment.created_at}</span>
                        <button class="comment-edit-btn"
                                onclick="startEditComment(${data.comment.id}, ${videoId})">编辑</button>
                    </div>
                    <div class="comment-content" id="cmt-content-${data.comment.id}"
                         data-original="${data.comment.content.replace(/"/g,'&quot;')}"
                    >${data.comment.content}</div>
                `;
                commentsList.insertBefore(div, commentsList.firstChild);
            }
            updateVideoStats(videoId, data);
        }
    } catch (error) {
        console.error('Comment error:', error);
    }
}

// 更新视频统计信息
function updateVideoStats(videoId, data) {
    const videoCard = document.getElementById(`video-${videoId}`);
    if (!videoCard) return;

    if (data.avg_rating !== undefined) {
        const el = videoCard.querySelector('.rating');
        if (el) el.textContent = `评分: ${data.avg_rating.toFixed(1)}`;
    }
    if (data.favorites_count !== undefined) {
        const el = videoCard.querySelector('.favorites');
        if (el) el.textContent = `收藏: ${data.favorites_count}`;
    }
    if (data.comments_count !== undefined) {
        const el = videoCard.querySelector('.comments');
        if (el) el.textContent = `讨论: ${data.comments_count}`;
    }
}

// 创建评论元素
function createCommentElement(comment) {
    const div = document.createElement('div');
    div.className = 'comment';
    div.innerHTML = `
        <span class="comment-author">${comment.author}:</span>
        <span class="comment-content">${comment.content}</span>
        <span class="comment-time">${comment.created_at}</span>
    `;
    return div;
}

// 添加点击模态框外部关闭功能
window.onclick = function(event) {
    const modal = document.getElementById('upload-modal');
    if (event.target == modal) {
        closeUploadModal();
    }
} 