import sqlite3
import pandas as pd
from datetime import datetime
import os

class DBManager:
    @staticmethod
    def backup_db():
        """数据库备份"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_dir = 'backups'
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        backup_path = f'{backup_dir}/backup_{timestamp}.db'
        
        conn = sqlite3.connect('instance/site.db')
        backup = sqlite3.connect(backup_path)
        conn.backup(backup)
        conn.close()
        backup.close()
        
    @staticmethod
    def export_table(table_name, format='csv'):
        """导出表数据"""
        conn = sqlite3.connect('instance/site.db')
        df = pd.read_sql(f"SELECT * FROM {table_name}", conn)
        
        export_dir = 'exports'
        if not os.path.exists(export_dir):
            os.makedirs(export_dir)
            
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if format == 'csv':
            df.to_csv(f'{export_dir}/{table_name}_{timestamp}.csv', index=False)
        elif format == 'json':
            df.to_json(f'{export_dir}/{table_name}_{timestamp}.json', orient='records')
