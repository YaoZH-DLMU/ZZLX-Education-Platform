import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils.db_manager import DBManager
import pandas as pd
import sqlite3
import json

def export_video_data(conn):
    """导出视频详细信息"""
    video_data = pd.read_sql("""
        SELECT 
            v.id,
            v.title,
            v.description,
            v.type,
            v.views,
            u.username as author,
            COUNT(DISTINCT vc.id) as comment_count,
            COUNT(DISTINCT vf.id) as favorite_count,
            COALESCE(AVG(vr.value), 0) as avg_rating
        FROM video v
        LEFT JOIN user u ON v.user_id = u.id
        LEFT JOIN video_comment vc ON v.id = vc.video_id
        LEFT JOIN video_favorite vf ON v.id = vf.video_id
        LEFT JOIN video_rating vr ON v.id = vr.video_id
        GROUP BY v.id
    """, conn)
    return video_data

def export_user_data(conn):
    """导出用户详细信息"""
    user_data = pd.read_sql("""
        SELECT 
            u.username,
            u.student_id,
            COUNT(DISTINCT v.id) as uploaded_videos,
            COUNT(DISTINCT vf.id) as favorited_videos,
            COUNT(DISTINCT vc.id) as comments_made,
            COUNT(DISTINCT p.id) as forum_posts
        FROM user u
        LEFT JOIN video v ON u.id = v.user_id
        LEFT JOIN video_favorite vf ON u.id = vf.user_id
        LEFT JOIN video_comment vc ON u.id = vc.user_id
        LEFT JOIN forum_posts p ON u.id = p.author_id
        GROUP BY u.id
    """, conn)
    return user_data

def export_comments(conn):
    """导出评论详细信息"""
    comments = pd.read_sql("""
        SELECT 
            vc.id,
            v.title as video_title,
            u.username as commenter,
            vc.content,
            vc.created_at
        FROM video_comment vc
        JOIN video v ON vc.video_id = v.id
        JOIN user u ON vc.user_id = u.id
        ORDER BY vc.created_at DESC
    """, conn)
    return comments

def main():
    print("开始导出数据...")
    
    # 确保导出目录存在
    if not os.path.exists('exports'):
        os.makedirs('exports')
    
    # 连接数据库
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app.db')
    conn = sqlite3.connect(db_path)
    
    try:
        # 导出视频数据
        video_data = export_video_data(conn)
        video_data.to_csv('exports/videos.csv', index=False)
        print("✓ 视频数据已导出")
        
        # 导出用户数据
        user_data = export_user_data(conn)
        user_data.to_csv('exports/users.csv', index=False)
        print("✓ 用户数据已导出")
        
        # 导出评论数据
        comments = export_comments(conn)
        comments.to_json('exports/comments.json', orient='records', indent=2)
        print("✓ 评论数据已导出")
        
    finally:
        conn.close()
    
    print("数据导出完成！")

if __name__ == "__main__":
    main()
