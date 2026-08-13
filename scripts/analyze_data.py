import os
import sys
# 添加项目根目录到 Python 路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils.data_analysis import DataAnalyzer
import pandas as pd
import matplotlib.pyplot as plt
import sqlite3

class VideoAnalyzer(DataAnalyzer):
    def analyze_video_engagement(self):
        """分析视频互动数据"""
        # 修改为正确的数据库路径
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app.db')
        print(f"正在连接数据库: {db_path}")
        
        conn = sqlite3.connect(db_path)
        
        try:
            # 先检查表结构
            tables = pd.read_sql("SELECT name FROM sqlite_master WHERE type='table';", conn)
            print("数据库中的表:", tables['name'].tolist())
            
            # 检查视频表数据
            test_query = "SELECT COUNT(*) as count FROM video"
            test_result = pd.read_sql(test_query, conn)
            print(f"数据库中的视频数量: {test_result['count'].iloc[0]}")
            
            # 获取视频数据
            video_data = pd.read_sql("""
                SELECT 
                    v.id,
                    v.title,
                    v.type,
                    u.username as author,
                    CAST(COALESCE(v.views, 0) AS INTEGER) as views,
                    COUNT(DISTINCT vc.id) as comment_count,
                    COUNT(DISTINCT vf.id) as favorite_count,
                    COALESCE(AVG(CAST(vr.value AS FLOAT)), 0) as avg_rating
                FROM video v
                LEFT JOIN user u ON v.user_id = u.id
                LEFT JOIN video_comment vc ON v.id = vc.video_id
                LEFT JOIN video_favorite vf ON v.id = vf.video_id
                LEFT JOIN video_rating vr ON v.id = vr.video_id
                GROUP BY v.id
            """, conn)
            
            # 检查是否有数据
            if video_data.empty:
                print("警告：没有找到视频数据")
                return None
            
            print(f"查询到的视频数据条数: {len(video_data)}")
            print("数据预览:")
            print(video_data.head())
            
            # 数据类型转换
            video_data['comment_count'] = pd.to_numeric(video_data['comment_count'])
            video_data['favorite_count'] = pd.to_numeric(video_data['favorite_count'])
            video_data['views'] = pd.to_numeric(video_data['views'])
            video_data['avg_rating'] = pd.to_numeric(video_data['avg_rating'])
            
            # 生成报告
            report = {
                'total_videos': len(video_data),
                'total_views': video_data['views'].sum(),
                'avg_rating': video_data['avg_rating'].mean(),
                'most_commented': video_data.nlargest(5, 'comment_count')[['title', 'author', 'comment_count']],
                'most_favorited': video_data.nlargest(5, 'favorite_count')[['title', 'author', 'favorite_count']],
                'highest_rated': video_data.nlargest(5, 'avg_rating')[['title', 'author', 'avg_rating']]
            }
            
            # 生成互动图表
            plt.figure(figsize=(15, 10))
            
            # 评论和收藏数图表
            plt.subplot(2, 1, 1)
            engagement_data = video_data[['title', 'comment_count', 'favorite_count']].set_index('title')
            engagement_data.plot(kind='bar', ax=plt.gca())
            plt.title('Video Engagement (Comments & Favorites)')
            plt.xticks(rotation=45, ha='right')
            plt.tight_layout()
            
            # 评分图表
            plt.subplot(2, 1, 2)
            ratings_data = video_data[['title', 'avg_rating']].set_index('title')
            ratings_data.plot(kind='bar', ax=plt.gca(), color='green')
            plt.title('Video Ratings')
            plt.xticks(rotation=45, ha='right')
            plt.tight_layout()
            
            if not os.path.exists('exports'):
                os.makedirs('exports')
            plt.savefig('exports/video_analysis.png', bbox_inches='tight', dpi=300)
            plt.close()
            
            return report
            
        except Exception as e:
            print(f"分析过程中出现错误: {str(e)}")
            return None
            
        finally:
            conn.close()

    def analyze_user_activity(self):
        """分析用户活动"""
        conn = sqlite3.connect('instance/site.db')
        
        try:
            user_activity = pd.read_sql("""
                SELECT 
                    u.username,
                    COUNT(DISTINCT v.id) as videos_uploaded,
                    COUNT(DISTINCT vc.id) as comments_made,
                    COUNT(DISTINCT vf.id) as videos_favorited
                FROM user u
                LEFT JOIN video v ON u.id = v.user_id
                LEFT JOIN video_comment vc ON u.id = vc.user_id
                LEFT JOIN video_favorite vf ON u.id = vf.user_id
                GROUP BY u.id
            """, conn)
            
            # 检查是否有数据
            if user_activity.empty:
                print("警告：没有找到用户活动数据")
                return pd.DataFrame()
            
            # 确保数值列的类型
            for col in ['videos_uploaded', 'comments_made', 'videos_favorited']:
                user_activity[col] = pd.to_numeric(user_activity[col])
            
            return user_activity
            
        except Exception as e:
            print(f"分析用户活动时出现错误: {str(e)}")
            return None
            
        finally:
            conn.close()
