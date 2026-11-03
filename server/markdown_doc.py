from pathlib import Path

def render_markdown(source: Path, target: Path):
    import markdown
    from bs4 import BeautifulSoup, NavigableString
    from docx import Document
    from docx.shared import Mm, Pt
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    body = BeautifulSoup(markdown.markdown(source.read_text(encoding='utf-8-sig'),
        extensions=['tables','fenced_code','sane_lists']), 'html.parser')
    for tag in body.find_all(['script','style','iframe','object','embed']): tag.decompose()
    for tag in body.find_all('img'): tag.replace_with('[图片：'+tag.get('alt','')+']')
    doc=Document()
    section=doc.sections[0]
    section.page_width,section.page_height=Mm(210),Mm(297)
    section.top_margin=section.bottom_margin=section.left_margin=section.right_margin=Mm(18)
    for name in ['Normal','Title','Heading 1','Heading 2','Heading 3','Heading 4','Heading 5','Heading 6']:
        style=doc.styles[name]
        style.font.name='WenQuanYi Micro Hei'
        style.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'),'WenQuanYi Micro Hei')
    doc.styles['Normal'].font.size=Pt(11)
    def runs(paragraph, node, bold=False, italic=False):
        if isinstance(node,NavigableString):
            run=paragraph.add_run(str(node));run.bold=bold;run.italic=italic
            return
        if node.name=='br': paragraph.add_run().add_break();return
        for child in node.children: runs(paragraph,child,bold or node.name in ['b','strong'],italic or node.name in ['em','i'])
    def blocks(parent):
        for node in parent.children:
            if isinstance(node,NavigableString):
                if node.strip(): doc.add_paragraph(str(node))
            elif node.name in ['h1','h2','h3','h4','h5','h6']:
                p=doc.add_heading(level=int(node.name[1]));runs(p,node)
            elif node.name=='table':
                rows=node.find_all('tr');count=max((len(r.find_all(['td','th'])) for r in rows),default=0)
                if count:
                    table=doc.add_table(rows=0,cols=count);table.style='Table Grid'
                    for row in rows:
                        cells=table.add_row().cells
                        for cell,content in zip(cells,row.find_all(['td','th'])): runs(cell.paragraphs[0],content,bold=content.name=='th')
            elif node.name in ['ul','ol']:
                for child in node.find_all('li',recursive=False):
                    p=doc.add_paragraph(style='List Number' if node.name=='ol' else 'List Bullet');runs(p,child)
            elif node.name=='pre':
                p=doc.add_paragraph();run=p.add_run(node.get_text());run.font.name='Liberation Mono';run.font.size=Pt(9)
            elif node.name in ['p','blockquote']:
                p=doc.add_paragraph();runs(p,node)
            else: blocks(node)
    blocks(body)
    doc.save(target)
