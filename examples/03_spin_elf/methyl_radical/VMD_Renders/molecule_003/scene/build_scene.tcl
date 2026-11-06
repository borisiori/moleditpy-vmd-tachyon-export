# Initial build: reconstruct the scene, snapshot it, render.
# Run from the project folder (vmd -dispdev none -e scene/build_scene.tcl)
if {[catch {source scene/scene.tcl} e]} {
  puts "ERROR building scene: $e"
  exit 1
}
save_state scene/scene.vmd
render Tachyon tachyon/scene.dat
exit
