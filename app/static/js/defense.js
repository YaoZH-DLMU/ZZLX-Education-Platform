// 视频评分功能
function rateVideo(videoId) {
    const ratingInput = document.getElementById(`rating-${videoId}`);
    const rating = parseInt(ratingInput.value);
    
    if (isNaN(rating) || rating < 1 || rating > 10) {
        alert('请输入1-10之间的评分');
        return;
    }

    fetch(SCRIPT_ROOT + `/api/videos/${videoId}/rate`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ rating: rating })
    })
    .then(response => response.json())
    .then(data => {
        const rateBtn = ratingInput.nextElementSibling;
        rateBtn.textContent = '已评分';
        rateBtn.classList.add('rated');
        ratingInput.value = '';
        
        // 更新统计信息
        const videoCard = document.getElementById(`video-${videoId}`);
        const stats = videoCard.querySelector('.video-stats');
        if (stats && data.avg_rating) {
            stats.children[0].textContent = `评分: ${data.avg_rating.toFixed(1)}`;
        }
    })
    .catch(error => {
        console.error('Rating error:', error);  // 只保留控制台日志
    });
}

// 视频收藏功能
function toggleFavorite(videoId) {
    fetch(SCRIPT_ROOT + `/api/videos/${videoId}/favorite`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        }
    })
    .then(response => response.json())
    .then(data => {
        const btn = document.querySelector(`#video-${videoId} .favorite-btn`);
        btn.textContent = data.is_favorited ? '已收藏' : '收藏';
        btn.classList.toggle('favorited', data.is_favorited);
        
        // 更新统计信息
        const stats = document.querySelector(`#video-${videoId} .video-stats`);
        if (stats && data.favorites_count !== undefined) {
            stats.children[1].textContent = `收藏: ${data.favorites_count}`;
        }
    });
}

// 上传表单处理
document.addEventListener('DOMContentLoaded', function() {
    const form = document.getElementById('upload-form');
    if (form) {
        form.addEventListener('submit', handleUpload);
    }
});

// 保持现有的上传处理函数
async function handleUpload(event) {
    event.preventDefault();
    const formData = new FormData(event.target);
    
    try {
        const response = await fetch(SCRIPT_ROOT + '/api/defense/upload', {
            method: 'POST',
            body: formData
        });
        
        if (response.ok) {
            window.location.href = '/cooperation';
        } else {
            alert('上传失败，请重试');
        }
    } catch (error) {
        console.error('Error:', error);
        alert('上传失败，请重试');
    }
}

// 文件选择验证
document.addEventListener('DOMContentLoaded', function() {
    const videoInput = document.getElementById('video');
    if (videoInput) {
        videoInput.addEventListener('change', function() {
            const file = this.files[0];
            if (file) {
                const fileType = file.type;
                const validTypes = ['video/mp4', 'video/avi', 'video/quicktime'];
                if (!validTypes.includes(fileType)) {
                    alert('请选择有效的视频文件（mp4, avi, mov）');
                    this.value = '';
                }
            }
        });
    }
});

// 页面加载完成时执行
document.addEventListener('DOMContentLoaded', function() {
    console.log('Defense page loaded');
    
    // 获取所有视频元素
    const videos = document.querySelectorAll('video');
    
    // 为每个视频添加加载事件监听
    videos.forEach(video => {
        video.addEventListener('loadeddata', function() {
            console.log('Video loaded:', video.src);
        });
        
        video.addEventListener('error', function() {
            console.log('Video load error:', video.src);
        });
    });
});

// 添加页面可见性变化监听
document.addEventListener('visibilitychange', function() {
    if (!document.hidden) {
        console.log('Page became visible, refreshing videos');
        // 当页面变为可见时，重新加载视频
        location.reload();
    }
});

// 添加新的功能处理代码
document.addEventListener('DOMContentLoaded', function() {
    // 评分功能
    document.querySelectorAll('.rate-btn').forEach(button => {
        button.addEventListener('click', async function() {
            const videoItem = this.closest('.video-item');
            const input = videoItem.querySelector('input[type="number"]');
            const videoId = videoItem.dataset.videoId;
            const rating = parseInt(input.value);

            if (rating < 1 || rating > 10) {
                alert('请输入1-10之间的分数');
                return;
            }

            try {
                const response = await fetch(SCRIPT_ROOT + `/api/videos/${videoId}/rate`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ rating: rating })
                });

                if (response.ok) {
                    const data = await response.json();
                    videoItem.querySelector('.rating-value').textContent = data.avg_rating;
                    input.value = '';
                }
            } catch (error) {
                console.error('Error:', error);
            }
        });
    });

    // 收藏功能
    document.querySelectorAll('.favorite-btn').forEach(button => {
        button.addEventListener('click', async function() {
            const videoId = this.dataset.videoId;

            try {
                const response = await fetch(SCRIPT_ROOT + `/api/videos/${videoId}/favorite`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    }
                });

                if (response.ok) {
                    const data = await response.json();
                    this.textContent = data.is_favorited ? '取消收藏' : '收藏';
                }
            } catch (error) {
                console.error('Error:', error);
                alert('操作失败，请重试');
            }
        });
    });

    // 评论功能
    document.querySelectorAll('.comment-btn').forEach(button => {
        button.addEventListener('click', async function() {
            const videoItem = this.closest('.video-item');
            const textarea = videoItem.querySelector('textarea');
            const videoId = videoItem.dataset.videoId;
            const content = textarea.value.trim();

            if (!content) {
                alert('请输入评论内容');
                return;
            }

            try {
                const response = await fetch(SCRIPT_ROOT + `/api/videos/${videoId}/comments`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ content: content })
                });

                if (response.ok) {
                    const data = await response.json();
                    const commentsList = videoItem.querySelector('.comments-list');
                    const newComment = document.createElement('div');
                    newComment.className = 'comment-item';
                    newComment.innerHTML = `
                        <span class="comment-author">${data.comment.author}</span>
                        <span class="comment-time">${data.comment.created_at}</span>
                        <div class="comment-content">${data.comment.content}</div>
                    `;
                    commentsList.insertBefore(newComment, commentsList.firstChild);
                    textarea.value = '';
                }
            } catch (error) {
                console.error('Error:', error);
            }
        });
    });
});

// 更新视频统计信息
function updateVideoStats(videoId, data) {
    const videoCard = document.getElementById(`video-${videoId}`);
    if (videoCard) {
        if (data.avg_rating) {
            const ratingValue = videoCard.querySelector('.rating-value');
            if (ratingValue) {
                ratingValue.textContent = data.avg_rating.toFixed(1);
            }
        }
    }
}

// 添加评论（空内容时触发语音输入）
async function addComment(videoId) {
    const commentInput = document.getElementById('comment-input-' + videoId)
        || document.querySelector('#video-' + videoId + ' .comment-input');
    const content = commentInput ? commentInput.value.trim() : '';
    if (!content) {
        if (typeof openVoiceCommentModal === 'function') {
            openVoiceCommentModal(videoId);
        }
        return;
    }
    await _submitComment(videoId, content, commentInput);
}

// 内部：实际提交评论
async function _submitComment(videoId, content, inputEl) {
    const SROOT = (typeof SCRIPT_ROOT !== 'undefined') ? SCRIPT_ROOT : '';
    try {
        const response = await fetch(SROOT + `/api/videos/${videoId}/comments`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: content })
        });
        const data = await response.json();
        if (response.ok) {
            if (inputEl) inputEl.value = '';
            const commentsList = document.getElementById('comments-' + videoId)
                || document.querySelector('#video-' + videoId + ' .comments-list');
            if (commentsList) {
                const div = document.createElement('div');
                div.className = 'comment-item';
                div.innerHTML = `
                    <span class="comment-author">${data.comment.author}</span>
                    <span class="comment-time">${data.comment.created_at}</span>
                    <div class="comment-content">${data.comment.content}</div>
                `;
                commentsList.insertBefore(div, commentsList.firstChild);
            }
            // 更新评论数
            const stats = document.querySelector('#video-' + videoId + ' .video-stats');
            if (stats) {
                const cmtSpan = Array.from(stats.querySelectorAll('span'))
                    .find(s => s.textContent.includes('评论'));
                if (cmtSpan) {
                    const cur = parseInt(cmtSpan.textContent.split(':').pop().trim()) || 0;
                    cmtSpan.textContent = '评论: ' + (cur + 1);
                }
            }
        }
    } catch (error) {
        console.error('Comment error:', error);
    }
}
