import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import epdf2jmd as app


def unit(
    uid,
    rect,
    text="A paragraph with enough words to identify a column. " * 2,
    kind="text",
):
    return app.Unit(uid, 1, rect, text, kind)


class ConversionTests(unittest.TestCase):
    def test_two_columns_with_wide_title_and_figure(self):
        units = [
            unit("r2", (310, 180, 560, 200)),
            unit("l1", (40, 100, 285, 120)),
            unit("r1", (310, 100, 560, 120)),
            unit("l2", (40, 180, 285, 200)),
            unit("title", (50, 30, 550, 60), "Title", "heading"),
            unit("figure", (40, 250, 560, 330), "", "figure"),
            unit("l3", (40, 350, 285, 370)),
            unit("r3", (310, 350, 560, 370)),
        ]
        self.assertEqual(
            [u.id for u in app.order_units(units, 600)],
            ["title", "l1", "l2", "r1", "r2", "figure", "l3", "r3"],
        )

    def test_fraction_fragments_merge_but_next_paragraph_stays(self):
        units = [
            unit("a", (70, 100, 220, 116), "f(x) = a", "equation"),
            unit("b", (160, 109, 220, 125), "b"),
            unit("c", (40, 140, 285, 180), "The following paragraph."),
        ]
        result = app.merge_overlapping_units(units, 600)
        self.assertEqual(len(result), 2)
        self.assertTrue(any("f(x)" in u.text and "\nb" in u.text for u in result))

    def test_math_delimiters_and_structural_errors(self):
        self.assertEqual(app.math_errors(r"$x \in [0,1)$"), [])
        for text in ["$$x=y", "${x$", "$}{x$", r"$\begin{matrix}x\end{cases}$"]:
            self.assertTrue(app.math_errors(text), text)
        self.assertEqual(app.math_errors(r"$\{x\}$ and $\frac{a}{b}$"), [])

    def test_equation_continuation_is_one_translation_unit(self):
        a = unit("a", (300, 100, 520, 120), "F = x", "equation")
        b = unit("b", (350, 127, 540, 138), "+ y + z")
        self.assertEqual(len(app.merge_equation_continuations([a, b])), 1)

    def test_multiline_title_is_one_translation_unit(self):
        a = unit("a", (40, 40, 550, 65), "An unrelated scientific title")
        b = unit("b", (80, 70, 500, 95), "continued on the next line")
        c = unit("c", (40, 180, 550, 220), "The body of a paper. " * 40)
        a.spans = [{"text": a.text, "size": 24}]
        b.spans = [{"text": b.text, "size": 24}]
        c.spans = [{"text": c.text, "size": 10}]
        merged = app.merge_title_lines([a, b, c], 800)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].kind, "title")

    def test_each_source_unit_must_survive_exactly_once(self):
        units = [
            unit("a", (0, 0, 1, 1), "Value 12."),
            unit("b", (0, 1, 1, 2), "x = y", "equation"),
        ]
        for result in [
            {"units": [{"id": "a", "markdown": "値は12である。"}]},
            {
                "units": [
                    {"id": "a", "markdown": "値は12。"},
                    {"id": "a", "markdown": "値は12。"},
                ]
            },
            {
                "units": [
                    {"id": "a", "markdown": "値は13。"},
                    {"id": "b", "markdown": "$x=y$"},
                ]
            },
            {
                "units": [
                    {"id": "a", "markdown": "値は12。"},
                    {"id": "b", "markdown": "x=y"},
                ]
            },
        ]:
            self.assertTrue(app.validate_translation(units, result)[1])
        good = {
            "units": [
                {"id": "a", "markdown": "値は12である。"},
                {"id": "b", "markdown": "$$x=y$$"},
            ]
        }
        self.assertEqual(app.validate_translation(units, good)[1], [])

    def test_assets_are_assembled_without_model_tokens(self):
        u = unit("a", (0, 0, 1, 1), "", "figure")
        u.asset = "assets/hash/figure.png"
        self.assertIn(
            "![[assets/hash/figure.png]]", app.assemble([[u]], {}, "[PDF](a.pdf)")
        )

    def test_appendix_after_references_keeps_its_position_and_translation(self):
        units = [
            unit("r", (0, 0, 1, 1), "References", "references_heading"),
            unit("a", (0, 1, 1, 2), "[1] An English reference.", "reference"),
            unit("b", (0, 2, 1, 3), "Appendix A: Details", "heading"),
        ]
        result = app.assemble([units], {"b": "## 付録A：詳細"}, "[PDF](a.pdf)")
        self.assertLess(result.index("[1]"), result.index("## 付録"))
        self.assertEqual(app.unit_kind("Appendix A: Details"), "heading")

    def test_case_insensitive_table_and_algorithm_crops(self):
        with pymupdf.open() as doc:
            p = doc.new_page(width=600, height=800)
            p.insert_text((80, 80), "Table 2: Evaluation")
            for y in [100, 120, 160]:
                p.draw_line((50, y), (280, y))
            p.insert_text((60, 115), "Method    Score")
            p.insert_text((60, 145), "Generic    42")
            p.insert_text((320, 80), "Algorithm 1: Generic procedure")
            for y in [65, 90, 200]:
                p.draw_line((310, y), (565, y))
            p.insert_text((320, 130), "Input: values")
            p.insert_text((320, 180), "Return: result")
            blocks = p.get_text("dict")["blocks"]
            regions, warnings = app.detect_regions(p, blocks)
            self.assertEqual(sorted(r.kind for r in regions), ["algorithm", "table"])
            self.assertEqual(warnings, [])
            self.assertGreater(
                next(r for r in regions if r.kind == "algorithm").bbox.y1, 199
            )

    def test_skip_and_failed_translation_keep_existing_final(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "different-topic.pdf"
            with pymupdf.open() as doc:
                p = doc.new_page()
                p.insert_text(
                    (60, 100), "A general paper about rainfall measurements in 2024."
                )
                doc.save(pdf)
            output = root / "out"
            output.mkdir()
            final = output / "[ja]翻訳.md"
            final.write_text("Existing valid translation", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                self.assertEqual(
                    app.main([str(pdf), "--output-dir", str(output), "--skip-codex"]), 0
                )
                with patch.object(
                    app,
                    "translate_pages",
                    side_effect=RuntimeError("simulated network failure"),
                ):
                    self.assertEqual(
                        app.main([str(pdf), "--output-dir", str(output)]), 1
                    )
            self.assertEqual(
                final.read_text(encoding="utf-8"), "Existing valid translation"
            )
            self.assertEqual(
                json.loads((output / "validation.json").read_text(encoding="utf8"))[
                    "status"
                ],
                "failed",
            )
            self.assertIn(
                "rainfall", (output / "[en]原文抽出.txt").read_text(encoding="utf-8")
            )

    def test_colliding_sanitized_names_have_different_outputs(self):
        a = Path("x" * 80 + "A.pdf").resolve()
        b = Path("x" * 80 + "B.pdf").resolve()
        self.assertEqual(app.safe_folder_name(a.stem), app.safe_folder_name(b.stem))
        self.assertNotEqual(app.default_output_dir(a), app.default_output_dir(b))

    def test_resume_uses_validated_cache_and_source_change_invalidates_it(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            args = app.parse_arguments(["--no-review"])
            pages = [[unit("a", (0, 0, 1, 1), "Value 12.")]]
            result = {"units": [{"id": "a", "markdown": "値は12である。"}]}
            with contextlib.redirect_stdout(io.StringIO()), patch.object(
                app, "run_codex", return_value=result
            ) as runner:
                app.translate_pages(pages, output, "hash", args)
                app.translate_pages(pages, output, "hash", args)
                self.assertEqual(runner.call_count, 1)
                pages[0][0].text = "The measured value is 12."
                app.translate_pages(pages, output, "hash", args)
                self.assertEqual(runner.call_count, 2)


if __name__ == "__main__":
    unittest.main()
