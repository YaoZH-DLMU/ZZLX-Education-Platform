# scripts/run_analysis.py - 运行分析的主脚本
from analyze_data import VideoAnalyzer
from export_data import main as export_data

def main():
    # 先导出数据
    print("Step 1: 导出数据")
    export_data()
    
    # 然后进行分析
    print("\nStep 2: 开始分析")
    analyzer = VideoAnalyzer()
    video_report = analyzer.analyze_video_engagement()
    
    # 打印分析结果
    print("\nStep 3: 分析结果")
    print("="*50)
    print("视频统计:")
    print(f"总视频数: {video_report['total_videos']}")
    print(f"总观看次数: {video_report['total_views']}")
    print(f"平均评分: {video_report['avg_rating']:.2f}")
    
    print("\n评论最多的视频:")
    print(video_report['most_commented'].to_string())
    
    print("\n收藏最多的视频:")
    print(video_report['most_favorited'].to_string())
    
    print("\n评分最高的视频:")
    print(video_report['highest_rated'].to_string())
    
    print("\n导出文件位置:")
    print("1. 视频详细数据: exports/videos.csv")
    print("2. 用户活动数据: exports/users.csv")
    print("3. 评论数据: exports/comments.json")
    print("4. 数据可视化: exports/video_analysis.png")
    print("="*50)

if __name__ == "__main__":
    main()
