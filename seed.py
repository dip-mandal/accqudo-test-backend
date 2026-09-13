import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import AsyncSessionLocal
from app.models.user import User, RoleEnum
from app.models.exam import Exam, Subject, Chapter, Topic
from app.models.question import Question, QuestionType
from app.models.test import Test, TestQuestion

async def seed_data():
    async with AsyncSessionLocal() as db:
        print("🌱 Seeding initial accqudo data...")

        # 1. Root Admin & Test Student
        admin_user = User(
            email="admin@accqudo.com",
            full_name="accqudo Admin",
            role=RoleEnum.SUPER_ADMIN,
            is_active=True
        )
        student_user = User(
            email="student@accqudo.com",
            full_name="Dip Student",
            role=RoleEnum.STUDENT,
            is_active=True
        )
        db.add_all([admin_user, student_user])
        await db.flush()

        # 2. Hierarchy 1: GATE CSE
        gate_exam = Exam(
            name="GATE Computer Science & IT",
            slug="gate-cse",
            description="Graduate Aptitude Test in Engineering for Computer Science"
        )
        db.add(gate_exam)
        await db.flush()

        ds_subject = Subject(exam_id=gate_exam.id, name="Data Structures & Algorithms")
        db.add(ds_subject)
        await db.flush()

        trees_chapter = Chapter(subject_id=ds_subject.id, name="Trees and Binary Search Trees")
        db.add(trees_chapter)
        await db.flush()

        avl_topic = Topic(chapter_id=trees_chapter.id, name="AVL Trees & Rotations")
        db.add(avl_topic)
        await db.flush()

        # Questions for GATE
        q1 = Question(
            topic_id=avl_topic.id,
            question_type=QuestionType.MCQ,
            question_text="What is the worst-case search time complexity in an AVL tree with n nodes?",
            options=[
                {"id": "A", "text": "O(n)"},
                {"id": "B", "text": "O(log n)"},
                {"id": "C", "text": "O(n log n)"},
                {"id": "D", "text": "O(1)"}
            ],
            evaluation_data={"correct": ["B"]},
            solution_text="AVL trees maintain strict height balance factor between -1 and +1, guaranteeing O(log n) height.",
            default_marks=1.0,
            default_negative_marks=0.33
        )

        q2 = Question(
            topic_id=avl_topic.id,
            question_type=QuestionType.NAT,
            question_text="What is the minimum number of nodes in an AVL tree of height 4? (Assume height of single node tree is 0)",
            options=None,
            evaluation_data={"exact": 12, "min": 12, "max": 12},
            solution_text="N(h) = N(h-1) + N(h-2) + 1. N(0)=1, N(1)=2, N(2)=4, N(3)=7, N(4)=12.",
            default_marks=2.0,
            default_negative_marks=0.0
        )

        db.add_all([q1, q2])
        await db.flush()

        # Create a Test for GATE
        gate_mock = Test(
            exam_id=gate_exam.id,
            title="GATE CSE: AVL Trees Topic Test 01",
            duration_minutes=30,
            total_marks=3.0,
            instructions={"rules": ["Calculator allowed", "Negative marking on MCQs only"]}
        )
        db.add(gate_mock)
        await db.flush()

        tq1 = TestQuestion(test_id=gate_mock.id, question_id=q1.id, order=1, marks=1.0, negative_marks=0.33)
        tq2 = TestQuestion(test_id=gate_mock.id, question_id=q2.id, order=2, marks=2.0, negative_marks=0.0)
        db.add_all([tq1, tq2])

        await db.commit()
        print("✅ Seeding completed successfully!")

if __name__ == "__main__":
    asyncio.run(seed_data())