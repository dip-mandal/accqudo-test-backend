from typing import Dict, Any, Tuple
from app.models.question import QuestionType

class EvaluationService:
    @staticmethod
    def evaluate_response(
        question_type: str,
        evaluation_data: Dict[str, Any],
        student_response: Any,
        marks: float,
        negative_marks: float
    ) -> Tuple[bool, float]:
        """
        Pure, deterministic evaluation function.
        Returns: (is_correct, obtained_marks)
        """
        if student_response is None or student_response == "" or student_response == []:
            return False, 0.0

        if question_type == QuestionType.MCQ:
            correct_answers = evaluation_data.get("correct", [])
            selected = student_response if isinstance(student_response, list) else [student_response]
            if selected == correct_answers:
                return True, marks
            return False, -abs(negative_marks)

        elif question_type == QuestionType.MSQ:
            correct_set = set(evaluation_data.get("correct", []))
            selected_set = set(student_response if isinstance(student_response, list) else [])
            if selected_set == correct_set:
                return True, marks
            return False, -abs(negative_marks)

        elif question_type == QuestionType.NAT:
            try:
                val = float(student_response)
                min_val = float(evaluation_data.get("min", evaluation_data.get("exact")))
                max_val = float(evaluation_data.get("max", evaluation_data.get("exact")))
                if min_val <= val <= max_val:
                    return True, marks
                return False, -abs(negative_marks)
            except (ValueError, TypeError):
                return False, -abs(negative_marks)

        return False, 0.0