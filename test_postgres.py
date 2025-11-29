"""Test PostgreSQL connection and show database info"""
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from datetime import datetime

# Load environment variables
load_dotenv()

def test_postgres_connection():
    """Test PostgreSQL database connection"""
    db_url = os.getenv('DATABASE_URL')
    
    if not db_url:
        print("❌ DATABASE_URL not found in .env file")
        return False
    
    # Fix postgres:// to postgresql://
    if db_url.startswith('postgres://'):
        db_url = db_url.replace('postgres://', 'postgresql://', 1)
    
    print("=" * 60)
    print("POSTGRESQL CONNECTION TEST")
    print("=" * 60)
    print(f"Database URL: {db_url[:50]}...")
    print()
    
    try:
        # Create engine
        engine = create_engine(db_url)
        
        # Test connection
        with engine.connect() as conn:
            # Get PostgreSQL version
            result = conn.execute(text("SELECT version()"))
            version = result.fetchone()[0]
            print(f"✓ Connected to PostgreSQL!")
            print(f"  Version: {version.split(',')[0]}")
            print()
            
            # Get database info
            result = conn.execute(text("SELECT current_database(), current_user"))
            db_name, user = result.fetchone()
            print(f"✓ Database Info:")
            print(f"  Database: {db_name}")
            print(f"  User: {user}")
            print()
            
            # List all tables
            result = conn.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public'
                ORDER BY table_name
            """))
            tables = [row[0] for row in result]
            
            print(f"✓ Tables Created ({len(tables)}):")
            for table in tables:
                print(f"  - {table}")
            print()
            
            # Check if admin user exists
            result = conn.execute(text("SELECT COUNT(*) FROM \"user\""))
            user_count = result.fetchone()[0]
            print(f"✓ Users: {user_count}")
            
            result = conn.execute(text("SELECT COUNT(*) FROM member"))
            member_count = result.fetchone()[0]
            print(f"✓ Members: {member_count}")
            
            result = conn.execute(text("SELECT COUNT(*) FROM payment"))
            payment_count = result.fetchone()[0]
            print(f"✓ Payments: {payment_count}")
            print()
            
            print("=" * 60)
            print("✓ PostgreSQL Database Ready!")
            print("=" * 60)
            print()
            print("Next Steps:")
            print("1. Run: python app.py")
            print("2. Login with admin/admin123")
            print("3. Start adding members!")
            print()
            print("Automatic backups are enabled:")
            print("- Backup every 6 hours")
            print("- Saved to: backups/")
            print("- Access Backup Manager in Dashboard")
            print("=" * 60)
            
            return True
            
    except Exception as e:
        print(f"❌ Connection failed!")
        print(f"   Error: {str(e)}")
        print()
        print("Troubleshooting:")
        print("1. Check DATABASE_URL in .env file")
        print("2. Ensure PostgreSQL is accessible")
        print("3. Verify credentials are correct")
        print("4. Check network/firewall settings")
        return False

if __name__ == '__main__':
    test_postgres_connection()
