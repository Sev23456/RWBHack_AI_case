import { ComponentFixture, TestBed } from '@angular/core/testing';

import { ZonesMap } from './zones-map';

describe('ZonesMap', () => {
  let component: ZonesMap;
  let fixture: ComponentFixture<ZonesMap>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ZonesMap]
    })
    .compileComponents();

    fixture = TestBed.createComponent(ZonesMap);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
