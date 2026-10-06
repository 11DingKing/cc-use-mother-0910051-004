import os
from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# 允许通过环境变量指定库文件，避免验收脚本/测试与运行实例互相删除同一数据库
SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./redscarf.db")

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

# SQLite 默认在首次写操作时才取 RESERVED 锁，"先查余额/库存再扣减"在并发下
# 会出现两个请求同时通过校验。这里让每个事务以 BEGIN IMMEDIATE 开始，
# 事务一开始即持有写锁：积分冻结与库存预占在同一事务内提交，天然原子且串行化。
@event.listens_for(engine, "connect")
def _sqlite_disable_pysqlite_begin(dbapi_connection, connection_record):
    dbapi_connection.isolation_level = None
    # 并发写时让后到的事务等待写锁而不是立即报 database is locked
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


@event.listens_for(engine, "begin")
def _sqlite_begin_immediate(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
