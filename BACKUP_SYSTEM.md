# Automatic Backup System

## Overview

Your Gym Management System now has a comprehensive automatic backup system that:

- ✅ Creates backups automatically every 6 hours
- ✅ Stores backups locally in the `backups/` folder
- ✅ Keeps the last 30 backups (auto-cleanup)
- ✅ Provides a Backup Manager UI in the dashboard
- ✅ Works with both SQLite (local) and PostgreSQL (cloud)

## Features

### 1. Automatic Backups

- **Frequency**: Every 6 hours (configurable)
- **Location**: `backups/` folder
- **Retention**: Last 30 backups kept automatically
- **On Startup**: Creates a backup when the server starts

### 2. Backup Manager Dashboard

Access the Backup Manager from the main dashboard:

- **Create Backup**: Manually trigger a new backup
- **List Backups**: View all available backups with timestamps and sizes
- **Download**: Download any backup to your computer
- **Restore**: Restore from a previous backup
- **Delete**: Remove old backups to save space

### 3. What's Backed Up

Each backup includes:

- **Database** (`gym.db` or PostgreSQL dump)
- **Members** (CSV export)
- **Payments** (CSV export)
- **Payment Transactions** (CSV export)
- **Audit Logs** (CSV export)

## Configuration

Edit `.env` file to configure backups:

```env
# Enable/disable automatic backups
AUTO_BACKUP_ENABLED=1

# Backup interval in hours (default: 6)
BACKUP_INTERVAL_HOURS=6

# Auto backup on login (optional)
AUTO_BACKUP_ON_LOGIN=0

# Backup destinations: local, email, drive
AUTO_BACKUP_DEST=local
```

## Using Backup Manager

### From Dashboard:

1. Click **"Backup Manager"** button
2. View all available backups
3. Options for each backup:
   - 📥 **Download**: Save backup to your computer
   - ⟲ **Restore**: Restore from this backup
   - 🗑️ **Delete**: Remove backup

### Manual Backup:

Click **"Create Backup"** button on dashboard to create an immediate backup.

### Quick Download:

Click **"Download Backup"** to instantly download the latest backup.

## Backup Files

Backup files are named with timestamps:

```
backup_20251129_210530.zip
```

Format: `backup_YYYYMMDD_HHMMSS.zip`

## Restoring from Backup

### Using Backup Manager (Recommended):

1. Open Backup Manager in dashboard
2. Find the backup you want to restore
3. Click the **Restore** button (⟲)
4. Confirm the action
5. Restart the application

### Manual Restore:

1. Stop the application
2. Extract `gym.db` from backup ZIP
3. Replace current database file
4. Restart the application

## PostgreSQL Database

Your system is now connected to:

- **Host**: dpg-d4lhsgmuk2gs738dt3v0-a.oregon-postgres.render.com
- **Database**: gym_management_f82n
- **User**: gym_management_f82n_user

### Benefits:

- ✅ Cloud-based (accessible anywhere)
- ✅ Professional database
- ✅ Better performance
- ✅ Automatic backups at database level
- ✅ Scalable for growth

## API Endpoints

### Create Backup

```
POST /admin/backup/create
```

### List All Backups

```
GET /admin/backup/list
```

### Download Latest Backup

```
GET /admin/backup/download
```

### Download Specific Backup

```
GET /admin/backup/download/<filename>
```

### Restore Backup

```
POST /admin/backup/restore/<filename>
```

### Delete Backup

```
DELETE /admin/backup/delete/<filename>
```

## Troubleshooting

### Backups Not Creating

1. Check `AUTO_BACKUP_ENABLED=1` in `.env`
2. Ensure `backups/` folder exists
3. Check disk space
4. Review application logs

### Cannot Restore

1. Stop the application first
2. Ensure backup file is valid
3. Check database permissions
4. Backup current database before restoring

### Large Backup Files

- Backups include all data and are compressed
- Old backups auto-delete (keeps last 30)
- Manually delete old backups if needed

## Security Notes

- ⚠️ Backup files contain sensitive data
- Store backups securely
- Don't share backup files publicly
- Use strong passwords
- Regular backups = data safety!

## Support

For issues or questions:

1. Check application logs
2. Test database connection: `python test_postgres.py`
3. Verify backup folder permissions
4. Review `.env` configuration

---

**Last Updated**: November 29, 2025
**Version**: 2.0 with PostgreSQL support
