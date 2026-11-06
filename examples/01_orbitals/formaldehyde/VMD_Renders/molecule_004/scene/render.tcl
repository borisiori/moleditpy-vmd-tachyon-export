# Re-render from the editable scene (does NOT touch scene.vmd,
# so GUI edits saved there survive). Run from the project folder:
#   vmd -dispdev none -e scene/render.tcl
if {[catch {source scene/scene.tcl} e]} {
  puts "ERROR building scene: $e"
  exit 1
}
render Tachyon tachyon/scene.dat
exit
