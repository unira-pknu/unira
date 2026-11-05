# UNIRA

부경대학교 학생이 학사·장학·학과 정보를 물으면 학교 공식 문서를 검색해 출처와 함께 답하는
RAG 서비스입니다. (개발 초기 이름: campus-rag)

- 서비스: https://unira-pknu.vercel.app
- 저장소: https://github.com/unira-pknu/unira

스캔 PDF를 포함한 전체 문서 전처리에는 **Tesseract OCR 실행 파일과 한국어(`kor`)·
영어(`eng`) 언어 데이터 설치가 필요합니다.** Python 패키지 설치만으로는 OCR 엔진이
설치되지 않습니다. 설치 후 `tesseract --version`, `tesseract --list-langs`로 확인하세요.
Windows 설치 및 실행 방법은 [전처리 가이드](docs/preprocessing/PREPROCESSING.md)를 참고하세요.

프로젝트 문서는 [`docs/`](docs/README.md)에서 기능별로 관리합니다.

- [프로젝트 개요·초기 설정](docs/project/README.md)
- [전체 실행 명령](docs/commands/README.md)
- [크롤러 실행 가이드](docs/crawlers/README.md)
- [학과·단과대학 크롤러](docs/crawlers/DEPARTMENTS.md)
- [백엔드](docs/backend/README.md) · [학교 서버 운영](docs/backend/SCHOOL_SERVER.md) · [프론트엔드](docs/frontend/README.md)
- [데이터 구조](docs/data/STRUCTURE.md)
- [평가](docs/evaluation/README.md) · [테스트](docs/testing/README.md)
