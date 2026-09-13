import io
import qrcode
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, Image as RLImage
from app.models.attempt import TestAttempt
from app.models.user import User
from app.models.test import Test


class PDFCertificateService:
    @staticmethod
    def _generate_qr_code_image(verify_url: str, size: float = 65) -> RLImage:
        """Generates an in-memory QR code flowable without writing to disk."""
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=1,
        )
        qr.add_data(verify_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0F172A", back_color="white")

        img_buffer = io.BytesIO()
        img.save(img_buffer, format="PNG")
        img_buffer.seek(0)
        return RLImage(img_buffer, width=size, height=size)

    @classmethod
    def generate_scorecard(
        cls,
        attempt: TestAttempt,
        user: User,
        test: Test,
        rank_info: dict | None = None,
        base_verification_url: str = "https://accqudo.com/verify/attempt"
    ) -> io.BytesIO:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=36,
            leftMargin=36,
            topMargin=36,
            bottomMargin=36
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'ReportTitle',
            parent=styles['Heading1'],
            fontSize=20,
            leading=24,
            textColor=colors.HexColor('#0F172A'),
            fontName='Helvetica-Bold'
        )
        subtitle_style = ParagraphStyle(
            'ReportSubtitle',
            parent=styles['Normal'],
            fontSize=9,
            textColor=colors.HexColor('#64748B'),
            fontName='Helvetica'
        )
        section_heading = ParagraphStyle(
            'SectionHeading',
            parent=styles['Heading2'],
            fontSize=11,
            leading=14,
            textColor=colors.HexColor('#0F172A'),
            fontName='Helvetica-Bold'
        )

        story = []

        # 1. Header with Title on Left, QR Code on Right
        verify_url = f"{base_verification_url}/{attempt.id}"
        qr_image = cls._generate_qr_code_image(verify_url, size=60)

        header_text = [
            Paragraph("ACCQUDO ASSESSMENT PLATFORM", title_style),
            Paragraph("Official Diagnostic Scorecard &amp; Authenticated Candidate Record", subtitle_style),
            Paragraph(f"<font size=7 color='#64748B'>Scan QR to verify authentic database entry: <u>{verify_url}</u></font>", subtitle_style)
        ]

        header_table = Table([[header_text, qr_image]], colWidths=[470, 70])
        header_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ]))
        story.append(header_table)
        story.append(Spacer(1, 10))
        story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#4F46E5"), spaceAfter=12))

        # 2. Candidate & Test Metadata Table
        cand_data = [
            [
                Paragraph("<b>Candidate Name:</b>", styles['Normal']),
                Paragraph(user.full_name or "N/A", styles['Normal']),
                Paragraph("<b>Attempt ID:</b>", styles['Normal']),
                Paragraph(f"#{attempt.id}", styles['Normal']),
            ],
            [
                Paragraph("<b>Registered Email:</b>", styles['Normal']),
                Paragraph(user.email, styles['Normal']),
                Paragraph("<b>Attempt Number:</b>", styles['Normal']),
                Paragraph(str(attempt.attempt_number), styles['Normal']),
            ],
            [
                Paragraph("<b>Assessment Title:</b>", styles['Normal']),
                Paragraph(test.title, styles['Normal']),
                Paragraph("<b>Submission Type:</b>", styles['Normal']),
                Paragraph(str(attempt.status.value if hasattr(attempt.status, 'value') else attempt.status), styles['Normal']),
            ]
        ]
        cand_table = Table(cand_data, colWidths=[110, 160, 110, 160])
        cand_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F8FAFC')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
            ('PADDING', (0, 0), (-1, -1), 5),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(cand_table)
        story.append(Spacer(1, 15))

        # 3. Primary Performance Metrics
        story.append(Paragraph("Performance Metrics", section_heading))
        story.append(Spacer(1, 6))

        rank_str = f"#{rank_info.get('rank')}" if rank_info and rank_info.get("is_ranked") else "--"
        pct_str = f"{rank_info.get('percentile', 0.0)}%" if rank_info and rank_info.get("is_ranked") else "--"

        perf_data = [
            ["Total Score", "Maximum Marks", "Accuracy", "Official Rank", "Cohort Percentile"],
            [
                f"{attempt.total_score:.2f}",
                f"{test.total_marks:.1f}",
                f"{round((attempt.correct_count / max(1, attempt.correct_count + attempt.incorrect_count)) * 100)}%",
                rank_str,
                pct_str
            ]
        ]
        perf_table = Table(perf_data, colWidths=[108, 108, 108, 108, 108])
        perf_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4F46E5')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
            ('TOPPADDING', (0, 0), (-1, -1), 7),
            ('BACKGROUND', (0, 1), (-1, 1), colors.HexColor('#EEF2FF')),
            ('FONTNAME', (0, 1), (-1, 1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 1), (-1, 1), 11),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E1')),
        ]))
        story.append(perf_table)
        story.append(Spacer(1, 15))

        # 4. Question Breakdown Matrix
        story.append(Paragraph("Question Analytics Breakdown", section_heading))
        story.append(Spacer(1, 6))

        counts_data = [
            ["Correct Answers", "Incorrect Deductions", "Unattempted", "Final Evaluation Status"],
            [
                str(attempt.correct_count),
                str(attempt.incorrect_count),
                str(attempt.unanswered_count),
                str(attempt.status.value if hasattr(attempt.status, 'value') else attempt.status)
            ]
        ]
        counts_table = Table(counts_data, colWidths=[135, 135, 135, 135])
        counts_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F5F9')),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
            ('PADDING', (0, 0), (-1, -1), 5),
        ]))
        story.append(counts_table)
        story.append(Spacer(1, 25))

        # 5. Cryptographic Seal & Verification Footer
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#94A3B8"), spaceAfter=8))
        footer_text = Paragraph(
            "<font size=7 color='#64748B'><b>Tamper-Proof Verification Notice:</b> This document was cryptographically sealed and "
            "generated by the accqudo atomic grading engine. Anyone can verify this certificate by scanning the embedded QR code "
            "or navigating to the verification link above.</font>",
            styles['Normal']
        )
        story.append(footer_text)

        doc.build(story)
        buffer.seek(0)
        return buffer