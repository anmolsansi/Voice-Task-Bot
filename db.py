from sqlalchemy import create_engine, Column, Integer, String, Date, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = "sqlite:///./tasks.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    raw_text = Column(String, nullable=False)
    task = Column(String, nullable=False)
    date = Column(Date, nullable=False)
    times_csv = Column(String, nullable=False)  # "10:00,13:00,15:00,18:00,20:00"
    is_range = Column(Boolean, default=False)
    completed = Column(Boolean, default=False)
    reminders = relationship("Reminder", backref="task")


class Reminder(Base):
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    run_at = Column(DateTime, nullable=False)
    sent = Column(Boolean, default=False)


def init_db():
    Base.metadata.create_all(bind=engine)
