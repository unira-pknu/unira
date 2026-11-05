# 수집 자료 전처리

전체 실행 절차는 [전체 전처리 가이드](../../docs/preprocessing/PREPROCESSING.md)에 정리되어 있습니다.

텍스트 추출(`extractors/`), 정제(`text_cleaning.py`), 청크 생성
(`file_preprocessing.py`), 규정 처리(`rule.py`), 이미지 검토 및 레이아웃 구현을
이 패키지에서 관리합니다. 이전 전처리 경로의 호환 파일은 제거했습니다. 벡터화와 DB 적재는
`scripts/vectorization/`에서 관리합니다.

추출기의 정식 import 경로는 `scripts.preprocessing.extractors`입니다.
이전 `scripts/extractors/` 폴더는 제거했으며 호환 모듈을 제공하지 않습니다.

기존 `scripts/ce/preprocessing.py`는 공통 파일 처리기로 통합하고 제거했습니다.
파일 단위 CLI는 `scripts.preprocessing.preprocess_files`를 사용하며
`--input-root`, `--output-root`를 명시합니다. `file_preprocessing`의 CLI도
동일한 공통 진입점으로 연결됩니다. 특정 학과를 기본 대상으로 선택하지 않습니다.

프로젝트 루트의 Python 3.12 가상환경에서 실행합니다.

```powershell
# 읽기 전용: 등록된 학과 전체, 본교 전체, 강의계획서, 규정의 형식별 개수
.venv/Scripts/python.exe -m scripts.preprocessing.collected
# 처리 경로 확인: 청크 생성 없음, 실행 로그 기록
.venv/Scripts/python.exe -m scripts.preprocessing.collected --dry-run
# 전체 범위 청크 생성: 기존 최신 결과는 건너뜀
.venv/Scripts/python.exe -m scripts.preprocessing.collected --run
# 기존 개별 데이터셋 실행
.venv/Scripts/python.exe -m scripts.preprocessing.preprocessing_pipeline --dataset ce --dry-run
```

`collected`는 크롤링·벡터화·DB 적재를 실행하지 않습니다. 상태, 백업 및 파생
폴더를 제외하고 `type`과 `url`이 있는 수집 JSON을 본문으로 처리합니다.
HTML은 JSON과 중복되므로 공통 처리에서 제외합니다. 규정은 기존 전용 로직으로
JSON·HTML·첨부를 처리합니다. 독립 이미지 OCR은 별도 검토 대상으로 남겨두며,
본교 본문 이미지 OCR 금지 정책을 변경하지 않습니다.

점검 결과는 `document_json`, `other_json`, `html_snapshot`, `extractable`,
`image_separate_review`, `unsupported`로 구분합니다. `extractable`은 형식 지원을
뜻하며 모든 파일의 내용 추출 성공을 보증하지 않습니다.

전처리 결과는 `files/preprocessed/<dataset>/`에 모읍니다. 본교 전체는
`files/preprocessed/pknu_main/`, 강의계획서는 `files/preprocessed/pknu_syllabus/`입니다.
강의계획서는 PDF 경로 기반 출처를 보존하며 과목 목록 메타데이터 조인은 아직 없습니다.
이 결과의 벡터화·DB 등록은 `scripts/vectorization/`의 별도 명령으로 수행합니다.

기존 `preprocessing_pipeline --dataset all`은 일일 수집 호환을 위해
CE·공지사항·대학생활·규정 4개 범위를 유지합니다. 전체 수집에는 `collected`를
사용하세요. `preprocessing_pipeline_recent`는 크롤링·벡터화·DB 적재도 포함합니다.

## 의존성과 처리 한계

스캔 PDF 처리를 위해 **Tesseract OCR을 별도로 설치**해야 합니다. 기본 OCR 언어는
`kor+eng`이므로 두 언어 데이터가 모두 필요합니다. Python 패키지 설치와는 별개입니다.

```powershell
winget install --id UB-Mannheim.TesseractOCR --exact --source winget
# 설치 후 새 터미널에서 실행 파일과 언어 데이터 확인
tesseract --version
tesseract --list-langs
```

설치 폴더(일반적으로 `C:\Program Files\Tesseract-OCR`)를 PATH에 추가해야 합니다.
언어 목록에 `kor`가 없으면 [공식 한국어 언어 데이터](https://github.com/tesseract-ocr/tessdata_fast/blob/main/kor.traineddata)를
설치 폴더의 `tessdata/kor.traineddata`에 저장하세요.

- JSON, PDF, DOCX, HWP/HWPX, CSV, XLS/XLSX, PPTX, TXT, ZIP을 지원합니다.
- PDF는 PyMuPDF, DOCX는 python-docx, XLS는 xlrd, PPTX는 python-pptx,
  HWP는 pyhwp, HTML은 beautifulsoup4가 필요합니다. XLSX는 표준 ZIP/XML
  추출기를 사용하므로 openpyxl이 필요하지 않습니다.
- DOC는 pywin32와 Microsoft Word가 필요합니다. 기존 변환기는 원본 옆에
  DOCX를 생성하므로 DOC 일괄 실행 전 변환 환경을 확인하세요.
- 스캔 PDF는 PATH의 Tesseract와 kor/eng 언어 데이터가 필요합니다.
  OCR이 불가능하면 내용 누락 또는 메타데이터만 생성될 수 있습니다.
- ZIP은 지원 멤버만 처리하며 멤버 수·크기를 제한합니다. 다른 압축 형식,
  구형 PPT, XLSB, 영상 등은 자동 처리하지 않습니다.
- 성공 로그만으로 본문 추출 성공을 판단하지 마세요. 결과의
  `used_metadata_fallback`, 청크 내용, ZIP 내부 실패도 확인해야 합니다.
- 읽기 전용 점검은 범위·형식·기본 의존성을 확인합니다. 암호화·손상·인코딩·
  텍스트 품질 검증은 실제 추출이 필요합니다. 이번 정리에서는 전체 일괄 전처리를
  실행하지 않았으며 표본 추출과 회귀 테스트로 검증했습니다.
